# Write two simulated detector responses to a native time–wavelength array.
# Run from the service checkout with an installed measure package.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop(
    "Usage: Rscript examples/make_dad_acquisition.R NEW_DIRECTORY",
    call. = FALSE
  )
}

grid <- expand.grid(
  wavelength = 224:287,
  time = seq(0, 6, length.out = 64),
  KEEP.OUT.ATTRS = FALSE
)
response <- with(
  grid,
  28 *
    exp(-((time - 2) / 0.40)^2 - ((wavelength - 251) / 9)^2) +
    19 * exp(-((time - 4) / 0.65)^2 - ((wavelength - 270) / 5)^2)
)
array <- measure::new_measure_nd_tbl(
  location_1 = grid$time,
  location_2 = grid$wavelength,
  value = response,
  dim_names = c("time", "wavelength"),
  dim_units = c("min", "nm")
)
record <- measure::measure_record(
  samples = tibble::tibble(sample_id = "two-responses"),
  injections = tibble::tibble(
    injection_id = "injection-1",
    sample_id = "two-responses"
  ),
  channels = tibble::tibble(channel_id = "dad")
)
acquisition <- measure::measure_acquisition(
  record,
  streams = tibble::tibble(
    stream_id = "dad-stream",
    injection_id = "injection-1",
    channel_id = "dad",
    kind = "nd",
    value_name = "absorbance",
    value_unit = "mAU"
  ),
  arrays = tibble::tibble(
    array_id = "dad-grid",
    stream_id = "dad-stream",
    .measures = list(array),
    .source_rows = list(seq_len(nrow(grid))),
    .saturated = list(rep(FALSE, nrow(grid)))
  )
)
measure::write_measure_acquisition(acquisition, args[[1]])
