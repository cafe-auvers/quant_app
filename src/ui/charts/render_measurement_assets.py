"""Static assets for the temporary right-drag percentage tool."""

RIGHT_DRAG_MEASUREMENT_CSS = """
                #measurement-overlay {
                    position: absolute;
                    inset: 0;
                    width: 100%;
                    height: 100%;
                    z-index: 8;
                    pointer-events: none;
                }
"""


RIGHT_DRAG_MEASUREMENT_JS = r"""
                const measurementCanvas = document.getElementById('measurement-overlay');
                const measurementContext = measurementCanvas.getContext('2d');
                let rightDragMeasurement = null;
                window.isRightDragMeasuring = false;

                function resizeMeasurementOverlay() {
                    const rect = measurementCanvas.getBoundingClientRect();
                    const ratio = window.devicePixelRatio || 1;
                    measurementCanvas.width = Math.max(1, Math.floor(rect.width * ratio));
                    measurementCanvas.height = Math.max(1, Math.floor(rect.height * ratio));
                    measurementContext.setTransform(ratio, 0, 0, ratio, 0, 0);
                    if (rightDragMeasurement) renderRightDragMeasurement();
                }

                function measurementPoint(event) {
                    const rect = measurementCanvas.getBoundingClientRect();
                    const x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
                    const y = Math.max(0, Math.min(rect.height, event.clientY - rect.top));
                    const price = candleSeries.coordinateToPrice(y);
                    if (price == null || !Number.isFinite(Number(price)) || Number(price) <= 0) {
                        return null;
                    }
                    return { x, y, price: Number(price) };
                }

                function clearRightDragMeasurement() {
                    rightDragMeasurement = null;
                    window.isRightDragMeasuring = false;
                    const rect = measurementCanvas.getBoundingClientRect();
                    measurementContext.clearRect(0, 0, rect.width, rect.height);
                    if (chartTooltip) chartTooltip.style.display = 'none';
                }

                function renderRightDragMeasurement() {
                    if (!rightDragMeasurement) return;
                    const rect = measurementCanvas.getBoundingClientRect();
                    const start = rightDragMeasurement.start;
                    const end = rightDragMeasurement.end;
                    const percent = ((end.price / start.price) - 1) * 100;
                    if (!Number.isFinite(percent)) return;

                    const color = percent > 0
                        ? '#22c55e'
                        : percent < 0
                            ? '#ef4444'
                            : '#e5e7eb';
                    const label = (percent > 0 ? '+' : '') + percent.toFixed(2) + '%';
                    measurementContext.clearRect(0, 0, rect.width, rect.height);
                    measurementContext.save();
                    measurementContext.strokeStyle = color;
                    measurementContext.fillStyle = color;
                    measurementContext.lineWidth = 2;
                    measurementContext.setLineDash([5, 5]);
                    measurementContext.beginPath();
                    measurementContext.moveTo(start.x, start.y);
                    measurementContext.lineTo(end.x, end.y);
                    measurementContext.stroke();
                    measurementContext.setLineDash([]);

                    for (const point of [start, end]) {
                        measurementContext.beginPath();
                        measurementContext.arc(point.x, point.y, 3.5, 0, Math.PI * 2);
                        measurementContext.fill();
                    }

                    measurementContext.font = '700 13px Arial, sans-serif';
                    measurementContext.textBaseline = 'middle';
                    const textWidth = measurementContext.measureText(label).width;
                    const labelWidth = textWidth + 16;
                    const labelHeight = 26;
                    const midpointX = (start.x + end.x) / 2;
                    const midpointY = (start.y + end.y) / 2;
                    const labelX = Math.max(
                        4,
                        Math.min(rect.width - labelWidth - 4, midpointX - labelWidth / 2)
                    );
                    const labelY = Math.max(
                        4,
                        Math.min(rect.height - labelHeight - 4, midpointY - labelHeight - 8)
                    );
                    measurementContext.fillStyle = 'rgba(15, 20, 25, 0.94)';
                    measurementContext.strokeStyle = color;
                    measurementContext.lineWidth = 1;
                    measurementContext.fillRect(labelX, labelY, labelWidth, labelHeight);
                    measurementContext.strokeRect(labelX, labelY, labelWidth, labelHeight);
                    measurementContext.fillStyle = color;
                    measurementContext.fillText(
                        label,
                        labelX + 8,
                        labelY + labelHeight / 2
                    );
                    measurementContext.restore();
                }

                pricePanel.addEventListener('contextmenu', event => {
                    event.preventDefault();
                    event.stopPropagation();
                }, true);

                pricePanel.addEventListener('mousedown', event => {
                    if (event.button !== 2) return;
                    const start = measurementPoint(event);
                    event.preventDefault();
                    event.stopPropagation();
                    if (!start) return;
                    rightDragMeasurement = { start, end: start };
                    window.isRightDragMeasuring = true;
                    if (chartTooltip) chartTooltip.style.display = 'none';
                    renderRightDragMeasurement();
                }, true);

                document.addEventListener('mousemove', event => {
                    if (!rightDragMeasurement) return;
                    event.preventDefault();
                    event.stopPropagation();
                    if ((event.buttons & 2) === 0) {
                        clearRightDragMeasurement();
                        return;
                    }
                    const end = measurementPoint(event);
                    if (!end) return;
                    rightDragMeasurement.end = end;
                    renderRightDragMeasurement();
                }, true);

                document.addEventListener('mouseup', event => {
                    if (!rightDragMeasurement || event.button !== 2) return;
                    event.preventDefault();
                    event.stopPropagation();
                    clearRightDragMeasurement();
                }, true);

                window.addEventListener('blur', clearRightDragMeasurement);
                window.addEventListener('resize', resizeMeasurementOverlay);
                if (typeof ResizeObserver !== 'undefined') {
                    new ResizeObserver(resizeMeasurementOverlay).observe(pricePanel);
                }
                setTimeout(resizeMeasurementOverlay, 0);
"""
