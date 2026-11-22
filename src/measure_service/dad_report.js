(function () {
  "use strict";
  const dataElement = document.getElementById("dad-data");
  const plane = JSON.parse(dataElement.textContent);
  const points = plane.points;
  const heatmap = document.getElementById("dad-heatmap");
  const timeInput = document.getElementById("inspect-time");
  const wavelengthInput = document.getElementById("inspect-wavelength");
  const formMessage = document.getElementById("inspect-message");
  const wavelengthChart = document.getElementById("wavelength-chart");
  const timeChart = document.getElementById("time-chart");
  const selection = plane.selection || null;
  const isRegion = Boolean(selection);

  function withinRange(value, range) {
    return range === null || (value >= range[0] && value <= range[1]);
  }

  function pointAt(index) {
    return points.find(function (point) { return point.point_index === index; });
  }

  function numberText(value) {
    return value === null ? "Unavailable" : JSON.stringify(value);
  }

  function stateText(point) {
    if (point.saturated === true) return "Saturated (declared)";
    if (point.value === null) return "Observed coordinate; value unavailable";
    if (point.saturated === null) return "Observed; saturation not declared";
    return "Observed";
  }

  function setText(id, value) {
    document.getElementById(id).textContent = value;
  }

  function normalized(value, low, high) {
    if (high === low) return 0.5;
    const span = high - low;
    if (Number.isFinite(span)) return (value - low) / span;
    return (value / 2 - low / 2) / (high / 2 - low / 2);
  }

  function ticks(low, high) {
    if (!Number.isFinite(low) || !Number.isFinite(high)) return [];
    if (low === high) return [low];
    const interval = (high / 4 - low / 4);
    const magnitude = 10 ** Math.floor(Math.log10(interval));
    const step = [1, 2, 5, 10].map(n => n * magnitude).find(n => n >= interval);
    const first = Math.ceil(low / step);
    const last = Math.floor(high / step);
    if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first > last || last - first > 8) {
      return [low, high];
    }
    return Array.from({length: last - first + 1}, (_, i) => (first + i) * step);

  }

  function svgElement(name) {
    return document.createElementNS("http://www.w3.org/2000/svg", name);
  }

  function addTick(svg, x, y, label, anchor) {
    const line = svgElement("line");
    line.setAttribute("x1", x);
    line.setAttribute("x2", x);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y + 5);
    line.setAttribute("class", "tick-mark");
    svg.appendChild(line);
    const text = svgElement("text");
    text.setAttribute("x", x);
    text.setAttribute("y", y + 20);
    text.setAttribute("text-anchor", anchor || "middle");
    text.setAttribute("class", "tick-label");
    text.textContent = String(Number(label.toPrecision(4)));
    svg.appendChild(text);
  }

  function addYTick(svg, x, y, label) {
    const line = svgElement("line");
    line.setAttribute("x1", x - 5);
    line.setAttribute("x2", x);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.setAttribute("class", "tick-mark");
    svg.appendChild(line);
    const text = svgElement("text");
    text.setAttribute("x", x - 9);
    text.setAttribute("y", y + 4);
    text.setAttribute("text-anchor", "end");
    text.setAttribute("class", "tick-label");
    text.textContent = String(Number(label.toPrecision(4)));
    svg.appendChild(text);
  }

  function clearChart(svg, message) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", "50%");
    text.setAttribute("y", "50%");
    text.setAttribute("text-anchor", "middle");
    text.textContent = message;
    svg.appendChild(text);
  }

  function renderChart(svg, rows, horizontalLabel, verticalLabel, coordinateLabel) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const width = 620;
    const height = 318;
    const left = 82;
    const right = 22;
    const top = 28;
    const bottom = 58;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = coordinateLabel;
    svg.appendChild(title);
    const desc = document.createElementNS("http://www.w3.org/2000/svg", "desc");
    desc.textContent = "Supplied DAD observations only. Missing values are shown as gaps; no interpolation is applied.";
    svg.appendChild(desc);
    const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    background.setAttribute("width", "100%");
    background.setAttribute("height", "100%");
    background.setAttribute("fill", "#fff");
    svg.appendChild(background);
    if (!rows.length) {
      clearChart(svg, "No observed rows at this coordinate");
      return;
    }
    const xValues = rows.map(function (row) { return row.x; });
    const finiteValues = rows.filter(function (row) { return row.point.value !== null; }).map(function (row) { return row.point.value; });
    const lowX = Math.min.apply(null, xValues);
    const highX = Math.max.apply(null, xValues);
    const lowY = finiteValues.length ? Math.min.apply(null, finiteValues) : null;
    const highY = finiteValues.length ? Math.max.apply(null, finiteValues) : null;
    function xMap(value) { return left + normalized(value, lowX, highX) * plotWidth; }
    function yMap(value) { return top + plotHeight - normalized(value, lowY, highY) * plotHeight; }
    const axis = svgElement("path");
    axis.setAttribute("d", "M" + left + " " + (top + plotHeight) + "V" + top + "M" + left + " " + (top + plotHeight) + "H" + (left + plotWidth));
    axis.setAttribute("class", "axis");
    svg.appendChild(axis);
    ticks(lowX, highX).forEach(function (value) {
      addTick(svg, xMap(value), top + plotHeight, value);
    });
    if (finiteValues.length) {
      ticks(lowY, highY).forEach(function (value) {
        addYTick(svg, left, yMap(value), value);
      });
    }
    const nullY = top + plotHeight + 22;
    if (rows.some(function (row) { return row.point.value === null; })) {
      const nullLabel = svgElement("text");
      nullLabel.setAttribute("x", left + plotWidth);
      nullLabel.setAttribute("y", nullY + 4);
      nullLabel.setAttribute("text-anchor", "end");
      nullLabel.setAttribute("class", "null-label");
      nullLabel.textContent = "missing value (outside numeric scale)";
      svg.appendChild(nullLabel);
    }
    rows.forEach(function (row) {
      const mark = svgElement("circle");
      mark.setAttribute("cx", xMap(row.x));
      mark.setAttribute("cy", row.point.value === null ? nullY : yMap(row.point.value));
      mark.setAttribute("r", "5");
      mark.setAttribute("class", row.point.value === null ? "missing" : (row.point.saturated === true ? "saturated" : "observed"));
      mark.setAttribute("tabindex", "0");
      mark.setAttribute("role", "button");
      mark.setAttribute("data-point-index", row.point.point_index);
      mark.setAttribute("aria-label", "point " + row.point.point_index + ", " + numberText(row.x) + ", value " + numberText(row.point.value));
      const tooltip = document.createElementNS("http://www.w3.org/2000/svg", "title");
      tooltip.textContent = "point " + row.point.point_index + "; " + numberText(row.x) + "; value " + numberText(row.point.value);
      mark.appendChild(tooltip);
      svg.appendChild(mark);
    });
    const xText = document.createElementNS("http://www.w3.org/2000/svg", "text");
    xText.setAttribute("x", left + plotWidth / 2);
    xText.setAttribute("y", height - 15);
    xText.setAttribute("text-anchor", "middle");
    xText.textContent = horizontalLabel;
    svg.appendChild(xText);
    const yText = document.createElementNS("http://www.w3.org/2000/svg", "text");
    yText.setAttribute("x", 15);
    yText.setAttribute("y", top + plotHeight / 2);
    yText.setAttribute("transform", "rotate(-90 15 " + (top + plotHeight / 2) + ")");
    yText.setAttribute("text-anchor", "middle");
    yText.textContent = verticalLabel;
    svg.appendChild(yText);
  }

  function renderTraces(time, wavelength) {
    setText("chrom-heading", (isRegion ? "Windowed chromatogram at " : "Chromatogram at ") + numberText(wavelength) + " " + plane.wavelength_unit);
    setText("spec-heading", (isRegion ? "Windowed spectrum at " : "Spectrum at ") + numberText(time) + " " + plane.time_unit);
    const wavelengthRows = points.filter(function (point) {
      return point.wavelength === wavelength;
    }).sort(function (a, b) { return a.time - b.time; }).map(function (point) {
      return {point: point, x: point.time};
    });
    const timeRows = points.filter(function (point) {
      return point.time === time;
    }).sort(function (a, b) { return a.wavelength - b.wavelength; }).map(function (point) {
      return {point: point, x: point.wavelength};
    });
    renderChart(wavelengthChart, wavelengthRows, "Time (" + plane.time_unit + ")", "Value (" + (plane.value_unit || "unit not declared") + ")", "Chromatogram at wavelength " + numberText(wavelength) + " " + plane.wavelength_unit);
    renderChart(timeChart, timeRows, "Wavelength (" + plane.wavelength_unit + ")", "Value (" + (plane.value_unit || "unit not declared") + ")", "Spectrum at time " + numberText(time) + " " + plane.time_unit);
  }

  function selectPoint(index) {
    const point = pointAt(index);
    if (!point) return;
    document.querySelectorAll(".heatmap-mark.selected").forEach(function (mark) { mark.classList.remove("selected"); });
    const mark = document.querySelector('.heatmap-mark[data-point-index="' + point.point_index + '"]');
    if (mark) mark.classList.add("selected");
    setText("selected-state", stateText(point));
    setText("selected-point", numberText(point.point_index));
    setText("selected-time", numberText(point.time) + " " + plane.time_unit);
    setText("selected-wavelength", numberText(point.wavelength) + " " + plane.wavelength_unit);
    setText("selected-value", numberText(point.value) + (plane.value_unit ? " " + plane.value_unit : ""));
    setText("selected-source-row", point.source_row === null ? "Unavailable" : numberText(point.source_row));
    setText("selected-saturated", point.saturated === null ? "Not declared" : String(point.saturated));
    timeInput.value = point.time;
    wavelengthInput.value = point.wavelength;
    formMessage.textContent = "Selected observed coordinate.";
    renderTraces(point.time, point.wavelength);
  }

  function inspectCoordinate(event) {
    if (event) event.preventDefault();
    if (timeInput.value.trim() === "" || wavelengthInput.value.trim() === "") {
      formMessage.textContent = "Enter both time and wavelength values.";
      setText("selected-state", "No coordinate selected");
      return;
    }
    const time = Number(timeInput.value);
    const wavelength = Number(wavelengthInput.value);
    if (!Number.isFinite(time) || !Number.isFinite(wavelength)) {
      formMessage.textContent = "Enter finite time and wavelength values.";
      setText("selected-state", "No coordinate selected");
      return;
    }
    const index = points.findIndex(function (point) {
      return point.time === time && point.wavelength === wavelength;
    });
    if (index >= 0) {
      selectPoint(points[index].point_index);
      return;
    }
    document.querySelectorAll(".heatmap-mark.selected").forEach(function (mark) { mark.classList.remove("selected"); });
    const insideRegion = !isRegion || (
      withinRange(time, selection.time_range) && withinRange(wavelength, selection.wavelength_range)
    );
    setText("selected-state", insideRegion ? (isRegion ? "Absent from selected window" : "Absent from sparse plane") : "Outside selected window");
    setText("selected-point", "None");
    setText("selected-time", numberText(time) + " " + plane.time_unit);
    setText("selected-wavelength", numberText(wavelength) + " " + plane.wavelength_unit);
    setText("selected-value", insideRegion ? "No observed row" : "Outside this export");
    setText("selected-source-row", insideRegion ? "Not applicable: coordinate absent" : "Not applicable: outside selected window");
    setText("selected-saturated", insideRegion ? "Not applicable: coordinate absent" : "Not applicable: outside selected window");
    formMessage.textContent = insideRegion
      ? (isRegion ? "The selected window has no observation at this coordinate." : "The acquisition has no observation at this coordinate.")
      : "This coordinate is outside the selected window.";
    renderTraces(time, wavelength);
  }

  function activatePoint(event) {
    if (event.type === "keydown" && event.key !== "Enter" && event.key !== " ") return;
    const mark = event.target.closest("[data-point-index]");
    if (!mark) return;
    event.preventDefault();
    const index = Number(mark.getAttribute("data-point-index"));
    selectPoint(index);
    // Slice points are redrawn when the selection changes. Keep keyboard focus on that point.
    if (event.type === "keydown" && !mark.isConnected) {
      event.currentTarget.querySelector('[data-point-index="' + index + '"]')?.focus();
    }
  }
  [heatmap, wavelengthChart, timeChart, document.getElementById("dad-points")].forEach(view => {
    view.addEventListener("click", activatePoint);
    view.addEventListener("keydown", activatePoint);
  });
  document.getElementById("inspect-form").addEventListener("submit", inspectCoordinate);
  if (points.length) selectPoint(points[0].point_index);
  else {
    setText("selected-state", "No observed point rows");
    clearChart(wavelengthChart, "No observed rows");
    clearChart(timeChart, "No observed rows");
  }
}());
