#!/usr/bin/env python3
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set
import tokenize

SAFE_EXCLUDED_DIRNAMES = {
    ".git", "venv", ".venv", "env", ".env", "build", "dist",
    "__pycache__", "node_modules",
}

DEFAULT_KEEP_PY_COMMENT_KEYWORDS = {
    "noqa", "type: ignore", "fmt: off", "fmt: on", "isort: skip",
    "pylint", "pragma", "pyright:", "mypy:", "coding:",
}

DEFAULT_KEEP_SH_COMMENT_KEYWORDS = {"shellcheck"}

ENCODING_COOKIE_RE = re.compile(r"coding[:=]\s*([-\.\w]+)")

def discover_source_files(paths: List[Path]) -> Dict[str, List[Path]]:
    """Находит исходные файлы (.py, .sh, .html, .css, .js) в указанных путях."""
    discovered: Dict[str, List[Path]] = {
        "py": [], "sh": [], "html": [], "css": [], "js": []
    }
    supported_suffixes = {
        ".py": "py", ".sh": "sh", ".html": "html",
        ".css": "css", ".js": "js",
    }

    for root_path in paths:
        if root_path.is_file():
            if root_path.suffix in supported_suffixes:
                file_type = supported_suffixes[root_path.suffix]
                discovered[file_type].append(root_path)
            continue
        if not root_path.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):

            dirnames[:] = [
                d for d in dirnames if d not in SAFE_EXCLUDED_DIRNAMES and not d.startswith(".")
            ]
            for filename in filenames:
                file_path = Path(dirpath) / filename
                if file_path.suffix in supported_suffixes:
                    file_type = supported_suffixes[file_path.suffix]
                    discovered[file_type].append(file_path)
    return discovered

def has_shebang(line: str) -> bool:
    """Проверяет, является ли строка shebang'ом."""
    return line.startswith("#!")

def has_encoding_cookie(line: str) -> bool:
    """Проверяет, содержит ли строка кодировку (PEP 263)."""
    return ENCODING_COOKIE_RE.search(line) is not None

def should_keep_py_comment(comment_text: str, keep_keywords: Set[str]) -> bool:
    """Определяет, следует ли сохранить комментарий в Python коде."""
    lower = comment_text.lower()
    return any(kw in lower for kw in keep_keywords)

def should_keep_sh_comment(comment_line: str, keep_keywords: Set[str]) -> bool:
    """Определяет, следует ли сохранить комментарий в Shell скрипте."""
    text_part = comment_line.lstrip()[1:].lstrip()
    lower = text_part.lower()
    return any(kw in lower for kw in keep_keywords)

def cleanup_empty_lines(text: str) -> str:
    """Убирает лишние пустые строки, оставляя не более одной подряд."""
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text.endswith("\n") and text:
        text += "\n"
    return text

def strip_python_comments(source: str, keep_keywords: Set[str]) -> str:
    """Удаляет ненужные комментарии из Python кода."""
    lines = source.splitlines(keepends=True)
    preserved_prefix: List[str] = []
    remaining_start_index = 0

    if lines and has_shebang(lines[0]):
        preserved_prefix.append(lines[0])
        remaining_start_index = 1
    if remaining_start_index < len(lines) and has_encoding_cookie(lines[remaining_start_index]):
        preserved_prefix.append(lines[remaining_start_index])
        remaining_start_index += 1

    remaining = "".join(lines[remaining_start_index:])
    tokens: List[tokenize.TokenInfo] = []
    try:
        tok_iter = tokenize.tokenize(io.BytesIO(remaining.encode("utf-8")).readline)
        for tok in tok_iter:
            if tok.type == tokenize.COMMENT:
                if should_keep_py_comment(tok.string, keep_keywords):
                    tokens.append(tok)
            else:
                tokens.append(tok)
    except tokenize.TokenError:
        return source

    try:
        processed = tokenize.untokenize(tokens)
        processed_text = processed.decode("utf-8") if isinstance(processed, bytes) else processed
    except Exception:
        return source

    return "".join(preserved_prefix) + cleanup_empty_lines(processed_text)

def strip_shell_comments(source: str, keep_keywords: Set[str]) -> str:
    """Удаляет ненужные комментарии из Shell скрипта."""
    lines = source.splitlines(keepends=True)
    if not lines:
        return ""
    new_lines: List[str] = []
    start_index = 0
    if has_shebang(lines[0]):
        new_lines.append(lines[0])
        start_index = 1
    for line in lines[start_index:]:
        stripped_line = line.lstrip()
        if stripped_line.startswith("#"):
            if should_keep_sh_comment(stripped_line, keep_keywords):
                new_lines.append(line)
        else:
            new_lines.append(line)
    return cleanup_empty_lines("".join(new_lines))

def strip_html_comments(source: str) -> str:
    """Удаляет комментарии <!-- ... --> из HTML кода."""
    cleaned = re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL)
    return cleanup_empty_lines(cleaned)

def strip_css_comments(source: str) -> str:
    """Удаляет комментарии /* ... */ из CSS кода."""
    cleaned = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return cleanup_empty_lines(cleaned)

def strip_js_comments(source: str) -> str:
    """Удаляет комментарии /* ... */ и // ... из JavaScript кода."""

    cleaned = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)

    cleaned = re.sub(r"(?<!:)//.*", "", cleaned)
    return cleanup_empty_lines(cleaned)

def write_if_changed(path: Path, new_content: str, dry_run: bool) -> bool:
    """Записывает контент, если он изменился."""
    try:
        old_content = path.read_text(encoding="utf-8")
    except Exception:
        old_content = None
    if old_content == new_content:
        return False
    if dry_run:
        return True
    path.write_text(new_content, encoding="utf-8")
    return True

def run_command(cmd: List[str]) -> int:
    """Выполняет команду в подпроцессе."""
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True).returncode
    except FileNotFoundError:
        print(f"[ERROR] Команда '{cmd[0]}' не найдена.", file=sys.stderr)
        return 127

def ensure_tool(cli_name: str, pip_name: Optional[str], allow_install: bool) -> bool:
    """Проверяет наличие инструмента и при необходимости устанавливает его."""
    if shutil.which(cli_name):
        return True
    if not pip_name:
        print(f"[WARN] Инструмент '{cli_name}' не найден в PATH. Установите его вручную.")
        return False
    if not allow_install:
        print(f"[WARN] Инструмент '{cli_name}' не найден. Пропускаю установку (--no-install).")
        return False
    print(f"[INFO] Устанавливаю '{pip_name}'...")
    code = run_command([sys.executable, "-m", "pip", "install", "-q", pip_name])
    if code != 0:
        print(f"[ERROR] Не удалось установить '{pip_name}'. Код: {code}", file=sys.stderr)
        return False
    return shutil.which(cli_name) is not None

def apply_formatting_tools(
    files: Dict[str, List[Path]], args: argparse.Namespace
) -> None:
    """Применяет линтеры и форматтеры к файлам."""
    py_files = files.get("py", [])
    sh_files = files.get("sh", [])
    allow_install = not args.no_install
    line_length = args.line_length

    if py_files:
        py_targets = [str(p) for p in py_files]
        if not args.skip_ruff and ensure_tool("ruff", "ruff", allow_install):
            print("[INFO] Запуск: ruff (удаление неиспользуемых импортов/переменных)")
            run_command([sys.executable, "-m", "ruff", "check", "--select", "F", "--fix", *py_targets])
        if not args.skip_isort and ensure_tool("isort", "isort", allow_install):
            print("[INFO] Запуск: isort (сортировка импортов)")
            run_command([sys.executable, "-m", "isort", "--profile", "black", f"--line-length={line_length}", *py_targets])
        if not args.skip_black and ensure_tool("black", "black", allow_install):
            print("[INFO] Запуск: black (форматирование кода)")
            run_command([sys.executable, "-m", "black", f"--line-length={line_length}", *py_targets])
    if sh_files:
        sh_targets = [str(p) for p in sh_files]
        if not args.skip_shellcheck and ensure_tool("shellcheck", None, allow_install):
            print("[INFO] Запуск: shellcheck (анализ скриптов)")
            run_command(["shellcheck", *sh_targets])
        if not args.skip_shfmt and ensure_tool("shfmt", None, allow_install):
            print("[INFO] Запуск: shfmt (форматирование скриптов)")
            run_command(["shfmt", "-w", "-i", "4", *sh_targets])

def main(argv: Iterable[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Очистка исходного кода (.py, .sh, .html, .css, .js): удаление комментариев, "
            "линтеры и форматтеры. По умолчанию обрабатывает текущий каталог."
        )
    )
    parser.add_argument("paths", nargs="*", type=Path, help="Пути к файлам/папкам (по умолчанию: .).")
    parser.add_argument("--dry-run", action="store_true", help="Показать изменения без записи на диск.")
    parser.add_argument("--only-comments", action="store_true", help="Только удалять комментарии, пропустить форматтеры.")
    parser.add_argument("--no-install", action="store_true", help="Не устанавливать автоматически отсутствующие инструменты.")
    parser.add_argument("--line-length", type=int, default=88, help="Длина строки для форматтеров.")

    web_group = parser.add_argument_group("Web (.html, .css, .js) options")
    web_group.add_argument("--skip-html", action="store_true", help="Пропустить .html файлы.")
    web_group.add_argument("--skip-css", action="store_true", help="Пропустить .css файлы.")
    web_group.add_argument("--skip-js", action="store_true", help="Пропустить .js файлы.")

    py_group = parser.add_argument_group("Python (.py) options")
    py_group.add_argument("--skip-ruff", action="store_true", help="Пропустить ruff.")
    py_group.add_argument("--skip-isort", action="store_true", help="Пропустить isort.")
    py_group.add_argument("--skip-black", action="store_true", help="Пропустить black.")
    py_group.add_argument("--keep-py-keywords", type=str, default=",".join(sorted(DEFAULT_KEEP_PY_COMMENT_KEYWORDS)),
                          help="Ключевые слова для сохранения комментариев в Python (через запятую).")

    sh_group = parser.add_argument_group("Shell (.sh) options")
    sh_group.add_argument("--skip-sh", action="store_true", help="Пропустить .sh файлы.")
    sh_group.add_argument("--skip-shellcheck", action="store_true", help="Пропустить shellcheck.")
    sh_group.add_argument("--skip-shfmt", action="store_true", help="Пропустить shfmt.")
    sh_group.add_argument("--keep-sh-keywords", type=str, default=",".join(sorted(DEFAULT_KEEP_SH_COMMENT_KEYWORDS)),
                          help="Ключевые слова для сохранения комментариев в Shell (через запятую).")

    args = parser.parse_args(list(argv) if argv is not None else None)
    targets = [p.resolve() for p in args.paths] if args.paths else [Path.cwd()]
    source_files = discover_source_files(targets)

    if not any(source_files.values()):
        print("[INFO] Не найдено поддерживаемых файлов для обработки.")
        return 0

    py_keep_kw = {kw.strip().lower() for kw in args.keep_py_keywords.split(",") if kw.strip()}
    sh_keep_kw = {kw.strip().lower() for kw in args.keep_sh_keywords.split(",") if kw.strip()}

    processors = {
        "py": (lambda src: strip_python_comments(src, py_keep_kw), not source_files.get("py")),
        "sh": (lambda src: strip_shell_comments(src, sh_keep_kw), args.skip_sh),
        "html": (strip_html_comments, args.skip_html),
        "css": (strip_css_comments, args.skip_css),
        "js": (strip_js_comments, args.skip_js),
    }

    changed_count = 0
    for file_type, (processor_func, skip) in processors.items():
        if skip:
            continue
        files_to_process = source_files.get(file_type, [])
        if not files_to_process:
            continue

        print(f"[INFO] Обработка {len(files_to_process)} .{file_type} файлов...")
        for file_path in files_to_process:
            try:
                original = file_path.read_text(encoding="utf-8")
                cleaned = processor_func(original)
                if write_if_changed(file_path, cleaned, args.dry_run):
                    if args.dry_run:
                        print(f"  - [ИЗМЕНИТСЯ] {file_path.relative_to(Path.cwd())}")
                    changed_count += 1
            except Exception as e:
                print(f"[ERROR] Не удалось обработать файл {file_path}: {e}", file=sys.stderr)

    if args.dry_run:
        print(f"\n[DRY-RUN] Будет изменено файлов после удаления комментариев: {changed_count}")
    else:
        print(f"\n[INFO] Изменено файлов (удаление комментариев): {changed_count}")

    if not args.only_comments:
        apply_formatting_tools(source_files, args)

    print("\n[DONE] Завершено.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
