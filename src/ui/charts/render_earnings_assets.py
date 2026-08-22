"""Static earnings overlay assets shared by generated chart pages."""

EARNINGS_CHART_CSS = """
                #earnings-upcoming-badge {
                    color: #fde68a;
                    background: rgba(245, 158, 11, 0.18);
                    border: 1px solid rgba(245, 158, 11, 0.5);
                    border-radius: 999px;
                    padding: 2px 8px;
                    font-size: 12px;
                    font-weight: 700;
                    white-space: nowrap;
                }
                #chart-tooltip {
                    display: none;
                    position: absolute;
                    left: 12px;
                    top: 10px;
                    z-index: 7;
                    max-width: min(310px, 62%);
                    padding: 7px 9px;
                    border: 1px solid rgba(107, 114, 128, 0.6);
                    border-radius: 4px;
                    background: rgba(15, 20, 25, 0.90);
                    color: #d1d5db;
                    font-size: 11px;
                    line-height: 1.35;
                    white-space: pre-line;
                    pointer-events: none;
                    user-select: none;
                }
                #earnings-event-layer {
                    position: absolute;
                    inset: 0;
                    z-index: 6;
                    overflow: hidden;
                    pointer-events: none;
                }
                .earnings-event-badge {
                    position: absolute;
                    bottom: 24px;
                    width: 30px;
                    height: 32px;
                    padding: 0;
                    border: 0;
                    outline: 0;
                    background: transparent;
                    color: #787b86;
                    cursor: pointer;
                    pointer-events: auto;
                    transform: translateX(-50%);
                }
                .earnings-event-badge:focus-visible {
                    filter: brightness(1.35);
                }
                .earnings-event-badge svg {
                    display: block;
                    width: 30px;
                    height: 32px;
                    overflow: visible;
                }
                .earnings-event-badge path {
                    fill: rgba(15, 20, 25, 0.94);
                    stroke: currentColor;
                    stroke-width: 2;
                    stroke-linejoin: round;
                    vector-effect: non-scaling-stroke;
                }
                .earnings-event-badge text {
                    fill: currentColor;
                    font-family: Arial, sans-serif;
                    font-size: 15px;
                    font-weight: 700;
                    text-anchor: middle;
                    dominant-baseline: middle;
                    pointer-events: none;
                    user-select: none;
                }
"""


EARNINGS_EVENT_RUNTIME_JS = r"""
                const earningsTooltipByTime = new Map(
                    earningsTooltips.map(item => [String(item.time), item.lines])
                );
                const reportedEarningsTooltips = earningsTooltips
                    .filter(item => item.reported)
                    .sort((left, right) => chartTimeSortValue(left.time) - chartTimeSortValue(right.time));
                const reportedEarningsSortValues = reportedEarningsTooltips.map(
                    item => chartTimeSortValue(item.time)
                );
                let activeCrosshairTime = null;
                let activeCrosshairCandle = null;

                function chartTimeSortValue(time) {
                    if (typeof time === 'number') return time;
                    if (typeof time === 'string') {
                        const parsed = Date.parse(time.slice(0, 10) + 'T00:00:00Z');
                        return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : Number.NEGATIVE_INFINITY;
                    }
                    if (time && typeof time === 'object') {
                        return Math.floor(Date.UTC(time.year, time.month - 1, time.day) / 1000);
                    }
                    return Number.NEGATIVE_INFINITY;
                }

                function carriedEarningsLines(time) {
                    const target = time == null
                        ? Number.POSITIVE_INFINITY
                        : chartTimeSortValue(time);
                    let low = 0;
                    let high = reportedEarningsSortValues.length - 1;
                    let selectedIndex = -1;
                    while (low <= high) {
                        const middle = (low + high) >> 1;
                        if (reportedEarningsSortValues[middle] <= target) {
                            selectedIndex = middle;
                            low = middle + 1;
                        } else {
                            high = middle - 1;
                        }
                    }
                    return selectedIndex >= 0
                        ? reportedEarningsTooltips[selectedIndex].lines
                        : [];
                }

                function setChartTooltipLines(lines) {
                    if (!chartTooltip) return;
                    if (!lines || lines.length === 0) {
                        chartTooltip.textContent = '';
                        chartTooltip.style.display = 'none';
                        return;
                    }
                    chartTooltip.textContent = lines.join('\n');
                    chartTooltip.style.display = 'block';
                }

                function restoreChartTooltip() {
                    const lines = [];
                    const candle = activeCrosshairCandle;
                    if (candle && candle.open != null) {
                        lines.push(
                            'O ' + Number(candle.open).toFixed(2) +
                            '  H ' + Number(candle.high).toFixed(2) +
                            '  L ' + Number(candle.low).toFixed(2) +
                            '  C ' + Number(candle.close).toFixed(2)
                        );
                    }
                    lines.push(...carriedEarningsLines(activeCrosshairTime));
                    setChartTooltipLines(lines);
                }

                function showExactEarningsTooltip(time) {
                    setChartTooltipLines(earningsTooltipByTime.get(String(time)) || []);
                }

                const earningsBadgeEntries = earningsEventLayer
                    ? earningsMarkers.map(marker => {
                        const badge = document.createElement('button');
                        badge.type = 'button';
                        badge.className = 'earnings-event-badge';
                        badge.dataset.earningsTime = String(marker.time);
                        badge.dataset.earningsKind = marker.reported ? 'reported' : 'expected';
                        badge.style.color = marker.color;
                        badge.setAttribute('aria-label', marker.detailText || 'Earnings');
                        badge.innerHTML =
                            '<svg viewBox="0 0 30 32" aria-hidden="true" focusable="false">' +
                            '<path d="M7 1 H23 L29 7 V23 H19 L15 31 L11 23 H1 V7 Z"></path>' +
                            '<text x="15" y="15.5">E</text></svg>';
                        const exactLines = earningsTooltipByTime.get(String(marker.time)) || [];
                        badge.title = exactLines.join('\n');
                        badge.addEventListener('mouseenter', () => showExactEarningsTooltip(marker.time));
                        badge.addEventListener('focus', () => showExactEarningsTooltip(marker.time));
                        badge.addEventListener('mouseleave', restoreChartTooltip);
                        badge.addEventListener('blur', restoreChartTooltip);
                        badge.addEventListener('mousedown', event => event.stopPropagation());
                        badge.addEventListener('click', event => {
                            event.stopPropagation();
                            showExactEarningsTooltip(marker.time);
                        });
                        earningsEventLayer.appendChild(badge);
                        return { marker, badge };
                    })
                    : [];
                let earningsBadgeAnimationFrame = null;

                function renderEarningsEventBadges() {
                    earningsBadgeAnimationFrame = null;
                    for (const entry of earningsBadgeEntries) {
                        const marker = entry.marker;
                        const badge = entry.badge;
                        const coordinate = chart.timeScale().timeToCoordinate(marker.time);
                        if (coordinate == null || coordinate < -16 || coordinate > container.clientWidth + 16) {
                            badge.style.display = 'none';
                            continue;
                        }
                        badge.style.display = 'block';
                        badge.style.left = coordinate + 'px';
                    }
                }

                function scheduleEarningsEventBadgeRender() {
                    if (earningsBadgeAnimationFrame != null) return;
                    earningsBadgeAnimationFrame = window.requestAnimationFrame(
                        renderEarningsEventBadges
                    );
                }

                chart.subscribeCrosshairMove(param => {
                    if (window.isRightDragMeasuring) {
                        if (chartTooltip) chartTooltip.style.display = 'none';
                        return;
                    }
                    if (!chartTooltip || !param || param.time == null || !param.point) {
                        activeCrosshairTime = null;
                        activeCrosshairCandle = null;
                        restoreChartTooltip();
                        return;
                    }
                    const lines = [];
                    const candle = param.seriesData.get(candleSeries);
                    activeCrosshairTime = param.time;
                    activeCrosshairCandle = candle || null;
                    if (candle && candle.open != null) {
                        lines.push(
                            `O ${Number(candle.open).toFixed(2)}  H ${Number(candle.high).toFixed(2)}  ` +
                            `L ${Number(candle.low).toFixed(2)}  C ${Number(candle.close).toFixed(2)}`
                        );
                    }
                    const eventLines = carriedEarningsLines(param.time);
                    if (eventLines.length > 0) lines.push(...eventLines);
                    if (lines.length === 0) {
                        chartTooltip.style.display = 'none';
                        return;
                    }
                    chartTooltip.textContent = lines.join('\n');
                    chartTooltip.style.display = 'block';
                });
                chart.timeScale().subscribeVisibleLogicalRangeChange(
                    scheduleEarningsEventBadgeRender
                );
                window.addEventListener('resize', scheduleEarningsEventBadgeRender);
                if (typeof ResizeObserver !== 'undefined') {
                    new ResizeObserver(scheduleEarningsEventBadgeRender).observe(container);
                }
                restoreChartTooltip();
"""
