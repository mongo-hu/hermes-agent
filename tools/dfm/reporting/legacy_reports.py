"""Compatibility JSON/Markdown report writers extracted during M1.2."""

from __future__ import annotations

from ..geometry.step import legacy_analyzer as legacy


def write_json_report(path: legacy.Path, result: dict[str, legacy.Any]) -> None:
    path.write_text(
        legacy.json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_markdown_report(
    path: legacy.Path, result: dict[str, legacy.Any], *, emit_stream: bool = False
) -> None:
    issues = result["issues"]
    stats = result["stats"]
    lines = [
        "# DFM 分析报告",
        "",
        f"- B-Rep 有效性: {('通过' if stats.get('valid_brep') else '存在问题')}",
        f"- 外包围尺寸(mm): {legacy.format_vec(stats.get('bbox_size_mm'))}",
        f"- 面/边/点数量: {stats['topology']['faces']} / {stats['topology']['edges']} / {stats['topology']['vertices']}",
        "",
    ]
    lines.extend(["## 发现的问题", ""])
    if not issues:
        lines.append("未发现超过当前阈值的 DFM 风险。")
    else:
        lines.extend(legacy.issue_table_header_lines())
        for issue in issues:
            lines.append(legacy.issue_table_row(issue))
            lines.extend(legacy.issue_image_rows(issue))
        lines.extend(["</tbody>", "</table>"])
    lines.extend([
        "",
        "## 输出图片",
        "",
        "- [overview.png](overview.png)",
        "- [model.png](model.png)",
        *(
            [
                "",
                "## 彩色 STEP 预览",
                "",
                f"- [{result['highlighted_step']['file']}]({result['highlighted_step']['file']})",
            ]
            if result.get("highlighted_step")
            else []
        ),
        *(
            ["", "## 彩色 STEP 导出警告", "", f"- {result['highlighted_step_error']}"]
            if result.get("highlighted_step_error")
            else []
        ),
        "",
        "说明: 该分析是自动几何启发式检查，最终量产结论仍需结合材料、工艺、模具结构和公差要求确认。",
        "",
    ])
    markdown = "\n".join(lines)
    path.write_text(markdown, encoding="utf-8-sig")
    if emit_stream:
        for line in lines:
            legacy.emit_report_delta(f"{line}\n")
