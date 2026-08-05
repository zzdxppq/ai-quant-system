/** 分时 SVG（7格×1.4% + 做T标注），index/review/ranking 共用 */
function buildMinuteFsChart(opts) {
    const p = opts.minutePayload
    if (!p || !p.bars || !p.bars.length) return null
            const bars = p.bars
            const livePre = opts.preCloseLive
            const pre = (livePre != null && livePre > 0) ? livePre : (Number(p.pre_close) || 0)
            const W = 900, H = 528
            const padL = 56, padR = 58, padT = 32, padB = 52
            const plotW = W - padL - padR
            const gap = 8
            const innerChart = H - padT - padB
            const priceH = (innerChart - gap) * 0.7
            const volH = (innerChart - gap) * 0.3
            const volTop = padT + priceH + gap
            const yBottom = H - padB
            const t930 = 9 * 60 + 30, t1130 = 11 * 60 + 30, t1300 = 13 * 60, t1500 = 15 * 60
            /* 东财式：上/下午各占一半，11:30–13:00 不占宽，接缝处仅标 11:30 */
            const mW = 0.5, aW = 0.5
            const xSeamPx = padL + mW * plotW

            function hmToMins(t) {
                const s = String(t || '').trim()
                if (!s) return null
                let h, mi
                const mm = s.match(/^(\d{1,2})\s*:\s*(\d{2})/)
                if (mm) {
                    h = parseInt(mm[1], 10)
                    mi = parseInt(mm[2], 10)
                } else if (/^\d{3,4}$/.test(s)) {
                    const pad = s.length === 3 ? '0' + s : s
                    h = parseInt(pad.slice(0, 2), 10)
                    mi = parseInt(pad.slice(2, 4), 10)
                } else {
                    const mmAll = s.matchAll(/\b(\d{1,2})\s*:\s*(\d{2})\b/g)
                    for (const m of mmAll) {
                        const hh = parseInt(m[1], 10)
                        const mmi = parseInt(m[2], 10)
                        if (hh < 9 || hh > 15 || mmi < 0 || mmi > 59) continue
                        if (hh === 9 && mmi < 30) continue
                        if (hh === 15 && mmi > 0) continue
                        return hh * 60 + mmi
                    }
                    return null
                }
                if (Number.isNaN(h) || Number.isNaN(mi)) return null
                if (mi < 0 || mi > 59 || h < 0 || h > 23) return null
                return h * 60 + mi
            }
            function fracFromMins(mins) {
                if (mins == null || mins < t930 || mins > t1500) return null
                if (mins <= t1130) return ((mins - t930) / (t1130 - t930)) * mW
                if (mins < t1300) return mW
                return mW + ((mins - t1300) / (t1500 - t1300)) * aW
            }
            const enriched = []
            for (let i = 0; i < bars.length; i++) {
                const b = bars[i]
                const mins = hmToMins(b.t)
                const frac = fracFromMins(mins)
                if (frac == null) continue
                const px = padL + frac * plotW
                const price = Number(b.p)
                const ap = Number(b.avg)
                const avg = Number.isFinite(ap) ? ap : price
                if (!Number.isFinite(price)) continue
                enriched.push({
                    i, b, mins, frac, px,
                    avg,
                    vol: Math.max(0, Number(b.vol_bar) || 0),
                })
            }
            enriched.sort((a, b) => a.mins - b.mins)
            if (!enriched.length) return null

            let spanFrac = 0
            if (enriched.length >= 2) {
                const frs = enriched.map((o) => o.frac)
                spanFrac = Math.max(...frs) - Math.min(...frs)
            }
            /* 时间解析丢条、或有效点挤在早盘窄窗 → 挤在左侧且无量感；按索引铺满 9:30–15:00 */
            const minEnriched = Math.max(10, Math.floor(bars.length * 0.55))
            const mustSpread =
                bars.length >= 8 &&
                (enriched.length < minEnriched || spanFrac < 0.17)
            if (mustSpread) {
                const fb = []
                const n = bars.length
                for (let i = 0; i < n; i++) {
                    const b = bars[i]
                    const price = Number(b.p)
                    if (!Number.isFinite(price)) continue
                    const ap = Number(b.avg)
                    const avg = Number.isFinite(ap) ? ap : price
                    const slot = Math.min(239, Math.floor((n <= 1 ? 0 : i / (n - 1)) * 239))
                    const mins = slot < 120 ? t930 + slot : t1300 + (slot - 120)
                    const fr2 = fracFromMins(mins)
                    if (fr2 == null) continue
                    const px = padL + fr2 * plotW
                    const frac = fr2
                    fb.push({
                        i, b, mins, frac, px, avg,
                        vol: Math.max(0, Number(b.vol_bar) || 0),
                    })
                }
                if (fb.length) {
                    let prev = 0
                    for (const o of fb) {
                        if (o.vol < 1e-9) {
                            const cum = Number(o.b.cum_lot)
                            if (Number.isFinite(cum) && cum >= prev) {
                                o.vol = cum - prev
                                prev = cum
                            }
                        }
                    }
                    enriched.splice(0, enriched.length, ...fb)
                }
            }

            const maxVol0 = Math.max(...enriched.map((o) => o.vol), 0)
            if (maxVol0 < 1e-9) {
                let prev = 0
                for (const o of enriched) {
                    const cum = Number(o.b.cum_lot)
                    if (Number.isFinite(cum) && cum >= prev) {
                        o.vol = cum - prev
                        prev = cum
                    }
                }
            }

            /* 通达信 7 格：昨收居中，上下各 7 格，每格 1.4%（±9.8%） */
            const GRID_PCT = 1.4
            const GRID_HALF = 7
            const gridSpan = GRID_HALF * 2
            const yFromPct = (pct) => padT + ((GRID_HALF - pct / GRID_PCT) / gridSpan) * priceH
            const preY = pre > 0 ? yFromPct(0) : null
            const yP = (price) => {
                if (pre <= 0) return padT + priceH * 0.5
                const pct = ((price / pre) - 1) * 100
                const clamped = Math.max(-GRID_HALF * GRID_PCT, Math.min(GRID_HALF * GRID_PCT, pct))
                return yFromPct(clamped)
            }

            function buildPathD(getter) {
                let d = ''
                const seamX = xSeamPx.toFixed(1)
                for (let k = 0; k < enriched.length; k++) {
                    const o = enriched[k]
                    const pr = getter(o)
                    const x = o.px
                    const y = yP(pr)
                    if (k === 0) {
                        d += `M ${x.toFixed(1)} ${y.toFixed(1)}`
                        continue
                    }
                    const p0 = enriched[k - 1]
                    const y0 = yP(getter(p0))
                    const gapLunch = o.mins >= t1300 && p0.mins <= t1130 && (o.mins - p0.mins) > 90
                    if (gapLunch) {
                        /* 午休：水平到接缝 → 垂直连下午首价 → 再画下午走势 */
                        d += ` L ${seamX} ${y0.toFixed(1)} L ${seamX} ${y.toFixed(1)} L ${x.toFixed(1)} ${y.toFixed(1)}`
                    } else {
                        d += ` L ${x.toFixed(1)} ${y.toFixed(1)}`
                    }
                }
                return d
            }
            const pricePathD = buildPathD((o) => Number(o.b.p))
            const avgPathD = buildPathD((o) => o.avg)

            const rawMaxVol = Math.max(...enriched.map((o) => o.vol), 0)
            const posVols = enriched.map((o) => o.vol).filter((v) => v > 0).sort((a, b) => a - b)
            let volCap = rawMaxVol
            if (posVols.length >= 8 && rawMaxVol > 0) {
                const p90 = posVols[Math.min(posVols.length - 1, Math.floor(posVols.length * 0.90))]
                if (rawMaxVol > p90 * 25 && p90 > 0)
                    volCap = Math.max(p90 * 8, rawMaxVol * 0.12)
            }

            const maxV = Math.max(...enriched.map((o) => Math.min(o.vol, volCap)), 1e-6)
            const barW = Math.max(plotW / 280, 0.65)
            let prevP = pre > 0 ? pre : Number(enriched[0]?.b?.p) || 0
            const volBars = enriched.map((o) => {
                const vdisp = Math.min(o.vol, volCap)
                const h = Math.max(3, (vdisp / maxV) * (volH - 10))
                const x = o.px - barW / 2
                const y = volTop + volH - h - 4
                const px = Number(o.b.p)
                const up = px >= prevP
                prevP = px
                return {
                    x: x.toFixed(1),
                    y: y.toFixed(1),
                    w: barW.toFixed(1),
                    h: h.toFixed(1),
                    color: up ? '#ef4444' : '#22c55e',
                }
            })

            const hGrid = []
            const yLabelsL = []
            const yLabelsR = []
            for (let g = GRID_HALF; g >= -GRID_HALF; g--) {
                const pct = g * GRID_PCT
                const y = yFromPct(pct)
                hGrid.push({ y, zero: g === 0 })
                const price = pre > 0 ? pre * (1 + pct / 100) : 0
                const col = g > 0 ? '#fca5a5' : g < 0 ? '#6ee7b7' : '#e2e8f0'
                yLabelsL.push({ x: 4, y: y + 4, text: price > 0 ? price.toFixed(2) : '—', col })
                const ps = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%'
                const colR = g > 0 ? '#fca5a5' : g < 0 ? '#6ee7b7' : '#94a3b8'
                yLabelsR.push({ x: W - 6, y: y + 4, text: ps, col: colR })
            }

            const volHGrid = []
            const vn = 3
            for (let k = 0; k <= vn; k++) {
                const frac = k / vn
                const y = volTop + frac * volH
                volHGrid.push({ y })
            }

            const vGrid = []
            const wantT = ['09:30', '10:00', '10:30', '11:00', '11:30', '13:30', '14:00', '14:30', '15:00']
            const seenX = new Set()
            for (const tag of wantT) {
                const mins = hmToMins(tag)
                const fr = fracFromMins(mins)
                if (fr == null) continue
                const x = padL + fr * plotW
                const xk = x.toFixed(1)
                if (seenX.has(xk)) continue
                seenX.add(xk)
                vGrid.push({ x: xk, y1: padT, y2: yBottom })
            }
            const xAt = (tag) => {
                const mins = hmToMins(tag)
                const fr = fracFromMins(mins)
                if (fr == null) return null
                return padL + fr * plotW
            }
            const xLabels = []
            for (const tag of wantT) {
                const x = xAt(tag)
                if (x != null) xLabels.push({ x, t: tag })
            }

            const td = p.trade_date || '—'
            const lastT = enriched[enriched.length - 1].b.t || ''
            const subtitle = `数据日 ${td} · 最新 ${lastT}`
            const headerLeft = `${opts.code || ''} ${(opts.name || p.name || '').trim()}`.trim()
            const headerTime = lastT || '—'

            const xLunchSeam = xSeamPx.toFixed(1)
            const bottomBarY = (yBottom - 0.5).toFixed(1)
            const clipPrice = { x: padL, y: padT, w: plotW, h: priceH }

            const timeToBar = new Map()
            for (const o of enriched) {
                timeToBar.set(String(o.b.t || ''), o)
            }
            const colorMap = {}
            const legend = Array.isArray(p.signal_legend) ? p.signal_legend : []
            for (const lg of legend) {
                if (lg && lg.kind) colorMap[lg.kind] = lg.color || '#94a3b8'
            }
            const signalMarkers = []
            const onMap = opts.signalOn || {}
            const sigList = (Array.isArray(p.signals) ? p.signals : []).filter(
                (s) => onMap[s.kind] !== false
            )
            for (const sig of sigList) {
                const o = timeToBar.get(String(sig.t || ''))
                if (!o) continue
                const x = o.px
                const price = Number(sig.price || o.b.p)
                if (!Number.isFinite(price)) continue
                const y = yP(price)
                const isBuy = sig.kind === 't_buy' || String(sig.kind || '').startsWith('buy')
                const color = colorMap[sig.kind] || (isBuy ? '#ef4444' : '#22c55e')
                const sz = 5.5
                const points = isBuy
                    ? `${x.toFixed(1)},${(y + sz * 1.6).toFixed(1)} ${(x - sz).toFixed(1)},${(y + sz * 3.6).toFixed(1)} ${(x + sz).toFixed(1)},${(y + sz * 3.6).toFixed(1)}`
                    : `${x.toFixed(1)},${(y - sz * 1.6).toFixed(1)} ${(x - sz).toFixed(1)},${(y - sz * 3.6).toFixed(1)} ${(x + sz).toFixed(1)},${(y - sz * 3.6).toFixed(1)}`
                signalMarkers.push({
                    points,
                    color,
                    side: isBuy ? 'buy' : 'sell',
                    label: sig.label || sig.kind,
                    t: sig.t,
                    pct: sig.pct != null ? sig.pct : 0,
                })
            }

            return {
                W, H, padL, padT, plotW, priceH, volTop, volH, preY, yBottom,
                hGrid, volHGrid, vGrid, yLabelsL, yLabelsR, volBars, xLabels,
                pricePathD, avgPathD, subtitle, headerLeft, headerTime,
                xLunchSeam, bottomBarY, clipPrice,
                signalMarkers, signalLegend: legend, gridPct: GRID_PCT,
            }
}

window.buildMinuteFsChart = buildMinuteFsChart
