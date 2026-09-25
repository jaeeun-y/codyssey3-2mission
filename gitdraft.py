#!/usr/bin/env python3
"""Generate reviewable commit and pull request drafts from the current Git worktree."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class GitChanges:
    """A compact snapshot of the worktree used as the AI prompt context."""

    status: str      #git 상태 출력
    diff: str        #변경 내용
    files: list[str] #변경 파일 경로 목록


def run_git(*args: str) -> str:
    """Run a Git command, returning stdout or a readable Git error."""
    try:
        result = subprocess.run(
            ["git", *args], text=True, capture_output=True, check=False
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git을 찾을 수 없습니다. Git 설치 상태를 확인하세요.") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} 실패: {detail}")
    return result.stdout


def require_repo_root() -> Path: #저장소 확인
    """Require the current directory to be exactly the repository root."""
    root = Path(run_git("rev-parse", "--show-toplevel")).resolve()
    if Path.cwd().resolve() != root:
        raise RuntimeError(f"프로젝트 루트에서 실행하세요: {root}")
    return root


def redact(text: str) -> str: #민감정보 마스킹
    """Mask common credentials and email addresses before sending a diff."""
    patterns = [
        (r"(?i)(\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*)['\"]?[^\s'\"]+", r"\1[REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_API_KEY]"),
        (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "[REDACTED_GITHUB_TOKEN]"),
        (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def collect_changes(safe_mode: bool) -> GitChanges:

    status = run_git("status", "--short", "--branch").strip()
    raw_status = run_git("status", "--porcelain") #컴퓨터용
    files = []
    for line in raw_status.splitlines(): #여러 줄로 이루어진 문자열을 리스트로 반환
        # Porcelain paths start at column 4; rename records can contain an arrow.
        path = line[3:].split(" -> ")[-1]
        files.append(path) #맨 마지막에 새로운 요소를 추가하는 함수

    has_head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], capture_output=True, check=False
    ).returncode == 0
    if has_head:
        diff = run_git("diff", "--no-ext-diff", "--unified=3", "HEAD", "--")
    else:
        # A newly initialized repository has no HEAD yet; staged content is
        # still useful context and `git diff HEAD` would fail in that state.
        diff = run_git("diff", "--cached", "--no-ext-diff", "--unified=3", "--")
    # `git diff HEAD` does not include untracked files. Include their text as an
    # addition-only patch so newly-created source files inform the draft too.
    untracked = [p for p in files if "??" in next((s[:2] for s in raw_status.splitlines() if s[3:].split(" -> ")[-1] == p), "")]
    for relative in untracked:
        path = Path(relative)
        if path.is_file():
            try:
                contents = path.read_text(encoding="utf-8")
            except (UnicodeError, OSError):
                contents = "[binary or unreadable file omitted]"
            diff += f"\n--- /dev/null\n+++ b/{relative}\n" + "".join(f"+{line}\n" for line in contents.splitlines())

    if safe_mode:
        files = files[:10]
        diff = "\n".join(diff.splitlines()[:200])
        diff = redact(diff)
        # Limit the path list as well as the patch sent to the API.
        included = set(files)
        diff = "\n".join(
            line for line in diff.splitlines()
            if not line.startswith(("diff --git ", "+++ b/", "--- a/"))
            or any(name in line for name in included)
        )
    return GitChanges(status=status, diff=diff, files=files)


def call_ai(prompt: str, model: str, temperature: float, max_tokens: int) -> str:
    """Call an OpenAI-compatible Chat Completions REST endpoint once."""
    api_key = os.getenv("AI_API_KEY")
    if not api_key:
        raise RuntimeError("AI_API_KEY 환경변수를 설정하세요.")
    endpoint = os.getenv("AI_API_BASE_URL", "https://api.openai.com/v1/chat/completions")
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": "You write concise, accurate Git commit and pull request drafts. Follow the requested format exactly. Never invent tests or motivations; state when context is missing."},
            {"role": "user", "content": prompt},
        ],
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"AI API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"AI API 네트워크 오류: {exc.reason}") from exc
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"AI API 응답 형식을 해석할 수 없습니다: {exc}") from exc


def make_prompt(kind: str, changes: GitChanges, reason: str, requirements: str) -> str:
    """Combine Git context, user intent, and exact output rules into one prompt."""
    if kind == "commit":
        format_rule = "Return only the commit message: a one-line imperative title (max 72 characters), optionally followed by a concise body. If there is a body, mention 1-3 changed files/modules or summarize 1-2 key changes as bullets."
    else:
        format_rule = "Return only a PR draft. First line: title (max 80 characters). Then sections exactly named Why, What, How to Test, each with at least one '- ' bullet. Do not claim tests were run unless the context says so."
    return f"""Task: create a {kind} draft.
User-provided reason/background: {reason or '(not provided; infer only from the diff)'}
Additional requirements: {requirements or '(none)'}
Changed files: {', '.join(changes.files) or '(none listed)'}
Git status:\n{changes.status}
Git diff:\n{changes.diff or '(empty diff)'}
Output rules: {format_rule}
Use the diff as evidence. Do not include secrets or repeat redacted values. Output plain text only."""


def clean_title(text: str, limit: int) -> str:
    """Normalize a generated title to one line and enforce the requested cap."""
    title = " ".join(text.strip().split())
    if len(title) > limit:
        title = title[: limit - 1].rstrip(" .,:;-") + "…"
    return title


def format_commit(raw: str) -> str:
    """Validate a commit title and preserve a useful optional body."""
    lines = [line.rstrip() for line in raw.strip().splitlines()]
    if not lines:
        raise RuntimeError("AI가 빈 커밋 메시지를 반환했습니다.")
    title = clean_title(lines[0].lstrip("# "), 72)
    body = "\n".join(lines[1:]).strip()
    return title + ("\n\n" + body if body else "")


def format_pr(raw: str) -> tuple[str, str]:
    """Ensure PR sections exist and each contains at least one bullet."""
    lines = [line.rstrip() for line in raw.strip().splitlines()]
    if not lines:
        raise RuntimeError("AI가 빈 PR 초안을 반환했습니다.")
    title = clean_title(lines[0].lstrip("# "), 80)
    body = "\n".join(lines[1:]).strip()
    canonical = ["Why", "What", "How to Test"]
    sections: dict[str, list[str]] = {name: [] for name in canonical}
    current = None
    for line in body.splitlines():
        heading = re.match(r"^#{0,3}\s*(Why|What|How to Test)\s*:?[ ]*$", line.strip(), re.I)
        if heading:
            current = next(name for name in canonical if name.lower() == heading.group(1).lower())
        elif current:
            sections[current].append(line)
    defaults = {
        "Why": "- Background is not specified; please add the motivation.",
        "What": "- See the changed files and diff summarized above.",
        "How to Test": "- Review the changes and run the relevant project checks.",
    }
    rendered = []
    for name in canonical:
        content = [line for line in sections[name] if line.strip()]
        if not any(re.match(r"\s*[-*+]\s+", line) for line in content):
            content.append(defaults[name])
        rendered.extend([f"## {name}", *content, ""])
    return title, "\n".join(rendered).strip()


def build_parser() -> argparse.ArgumentParser:
    """Define shared API options and the commit/pr subcommands."""
    parser = argparse.ArgumentParser(description="Git diff에서 commit/PR 초안을 생성합니다.")
    parser.add_argument("--model", default=os.getenv("AI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--safe-mode", action="store_true", help="diff를 200줄/10파일로 제한하고 키·이메일을 마스킹")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("commit", "pr"):
        command = sub.add_parser(name, help=f"{name} 초안 생성")
        command.add_argument("--reason", default="", help="변경 배경 또는 이유")
        command.add_argument("--requirements", default="", help="팀 템플릿/표현 요구사항")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Run one end-to-end draft flow, using exactly one API request."""
    args = build_parser().parse_args(argv)
    try:
        require_repo_root()
        changes = collect_changes(args.safe_mode)
        if not changes.files:
            print("변경 사항이 없습니다.")
            return 0
        if not changes.diff.strip():
            print("변경 파일은 있지만 diff에 내용이 없습니다(바이너리/무시 파일일 수 있음).")
            return 0
        if not 0 <= args.temperature <= 2:
            raise RuntimeError("temperature는 0부터 2 사이여야 합니다.")
        if args.max_tokens < 1:
            raise RuntimeError("max-tokens는 1 이상이어야 합니다.")
        prompt = make_prompt(args.command, changes, args.reason, args.requirements)
        print(f"AI API 호출: 1회 | model={args.model} | temperature={args.temperature} | max_tokens={args.max_tokens}")
        generated = call_ai(prompt, args.model, args.temperature, args.max_tokens)
        print(f"\n변경 파일 ({len(changes.files)}): " + ", ".join(changes.files))
        if args.command == "commit":
            print("\n========== Commit Message ==========")
            print(format_commit(generated))
        else: 
            title, body = format_pr(generated)
            print("\n========== Pull Request ==========")
            print(f"제목: {title}\n\n{body}")
        return 0
    except RuntimeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
