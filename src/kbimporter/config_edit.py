from __future__ import annotations

import re
from pathlib import Path


def format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_toml_value(path: Path, key_path: str, value) -> bool:
    """设置 TOML 配置中的键值，支持 a.b 形式的 section.key。

    - 已存在则替换；不存在则在 section 末尾（或文件末尾新建 section）追加。
    - 只做行级编辑，不重排注释。
    """
    parts = key_path.split(".")
    section = parts[0]
    key = parts[1] if len(parts) > 1 else parts[0]
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    changed = False

    sec_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped[1:-1].strip() == section:
                sec_idx = i
                break

    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    if sec_idx is not None:
        for j in range(sec_idx + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                lines.insert(j, f"{key} = {format_value(value)}")
                changed = True
                break
            if pattern.match(lines[j]):
                lines[j] = f"{key} = {format_value(value)}"
                changed = True
                break
        else:
            lines.append(f"{key} = {format_value(value)}")
            changed = True
    else:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(f"[{section}]")
        lines.append(f"{key} = {format_value(value)}")
        changed = True

    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed

