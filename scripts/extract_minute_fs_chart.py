"""One-off: extract buildMinuteFsChart from index.html to static/js/minute-fs-chart.js"""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
src = (root / "src/static/index.html").read_text(encoding="utf-8")
start = src.index("            const bars = p.bars")
end = src.index("        // ============= 大图 computed", start)
core = src[start:end]
header = """/** 分时 SVG（7格×1.4% + 做T标注），index/review/ranking 共用 */
function buildMinuteFsChart(opts) {
    const p = opts.minutePayload
    if (!p || !p.bars || !p.bars.length) return null
"""
core = core.replace(
    "const livePre = klineLiveDisplay.value.pre_close",
    "const livePre = opts.preCloseLive",
)
core = core.replace(
    "const pre = (livePre != null && livePre > 0) ? livePre : (Number(p.pre_close) || 0)",
    "const pre = (livePre != null && livePre > 0) ? livePre : (Number(p.pre_close) || 0)",
)
core = core.replace(
    "minuteSignalOn.value || {}",
    "opts.signalOn || {}",
)
core = core.replace(
    "const headerLeft = `${m.code || ''} ${(m.name || p.name || '').trim()}`.trim()",
    "const headerLeft = `${opts.code || ''} ${(opts.name || p.name || '').trim()}`.trim()",
)
footer = "\n}\nwindow.buildMinuteFsChart = buildMinuteFsChart\n"
out = root / "src/static/js/minute-fs-chart.js"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(header + core + footer, encoding="utf-8")
print("wrote", out, "lines", len((header + core + footer).splitlines()))
