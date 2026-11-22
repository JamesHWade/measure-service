# Compare a selected detector region with the full acquisition.
# Run after the dense-run and first-response commands in docs/dad-plane.md.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 3L) {
  stop("Usage: Rscript examples/plot_dad_region.R BUNDLE REPORT NEW_PNG")
}
if (file.exists(args[[3]])) {
  stop("Choose a new figure path.")
}

region <- jsonlite::fromJSON(file.path(args[[2]], "evidence.json"))
acq <- measure::read_measure_acquisition(args[[1]])
array <- acq$arrays$.measures[[match(region$array_id, acq$arrays$array_id)]]
time <- array[[paste0("location_", region$time_axis)]]
wavelength <- array[[paste0("location_", 3L - region$time_axis)]]
bounds <- region$selection
stopifnot(
  identical(region$profile, "measure-dad-region/1"),
  length(bounds$time_range) == 2L,
  length(bounds$wavelength_range) == 2L
)
index <- which(
  time >= bounds$time_range[1] &
    time <= bounds$time_range[2] &
    wavelength >= bounds$wavelength_range[1] &
    wavelength <= bounds$wavelength_range[2]
)
stopifnot(
  length(index) > 0L,
  identical(index, region$points$point_index),
  identical(as.double(array$value[index]), as.double(region$points$value)),
  all(time[index] == region$points$time),
  all(wavelength[index] == region$points$wavelength)
)

palette <- grDevices::hcl.colors(64, "YlGnBu", rev = TRUE)
signal_limits <- range(array$value, finite = TRUE)
stopifnot(all(is.finite(signal_limits)))
color <- function(x) {
  if (signal_limits[1] == signal_limits[2]) {
    return(ifelse(is.finite(x), palette[32], NA_character_))
  }
  palette[cut(
    x,
    breaks = seq(signal_limits[1], signal_limits[2], length.out = 65),
    include.lowest = TRUE
  )]
}
plot_plane <- function(x, y, signal, title) {
  plot(
    x,
    y,
    pch = 15,
    cex = 0.75,
    col = color(signal),
    main = title,
    xlab = paste0("Time (", region$time_unit, ")"),
    ylab = paste0("Wavelength (", region$wavelength_unit, ")"),
    las = 1,
    bty = "l"
  )
}
grDevices::png(args[[3]], width = 1400, height = 570, res = 120)
par(
  mfrow = c(1, 2),
  mar = c(4, 4.5, 2.6, 1),
  oma = c(1.5, 0, 0, 0),
  fg = "#303B43"
)
plot_plane(time, wavelength, array$value, "Full detector run")
rect(
  bounds$time_range[1],
  bounds$wavelength_range[1],
  bounds$time_range[2],
  bounds$wavelength_range[2],
  border = "#B73537",
  lwd = 2
)
with(
  region$points,
  plot_plane(time, wavelength, value, "Selected observations")
)
mtext(
  sprintf(
    "Both panels use the same detector scale (%.0f–%.0f %s).",
    signal_limits[1],
    signal_limits[2],
    region$value_unit
  ),
  side = 1,
  outer = TRUE,
  line = 0,
  cex = 0.85
)
invisible(grDevices::dev.off())
