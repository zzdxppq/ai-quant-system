from pathlib import Path

root = Path(__file__).resolve().parents[1]
p = root / "src/static/index.html"
text = p.read_text(encoding="utf-8")
start = text.index("        const minuteFsChart = computed(() => {")
end = text.index("        // ============= 大图 computed", start)
wrapper = """        const minuteFsChart = computed(() => {
            const m = klineModal.value
            return window.buildMinuteFsChart?.({
                minutePayload: m.minutePayload,
                code: m.code,
                name: m.name,
                preCloseLive: klineLiveDisplay.value.pre_close,
                signalOn: minuteSignalOn.value,
            }) ?? null
        })

"""
text = text[:start] + wrapper + text[end:]
if "/static/js/minute-fs-chart.js" not in text:
    text = text.replace(
        '<script src="https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js"></script>',
        '<script src="https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js"></script>\n    <script src="/static/js/minute-fs-chart.js"></script>',
        1,
    )
p.write_text(text, encoding="utf-8")
print("patched index.html")
