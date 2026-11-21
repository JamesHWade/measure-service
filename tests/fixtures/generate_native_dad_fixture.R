#!/usr/bin/env Rscript

# Regenerate the compact synthetic diode-array acquisition used by the worker
# tests. The native writer refuses to replace an existing destination, so pass
# a new directory while developing or remove the old output deliberately.
suppressPackageStartupMessages(library(measure))

arguments <- commandArgs(trailingOnly = TRUE)
destination <- if (length(arguments) == 0L) {
  file.path("tests", "fixtures", "native_dad_v1")
} else {
  arguments[[1L]]
}

if (file.exists(destination)) {
  stop("Refusing to replace an existing fixture destination: ", destination)
}
dir.create(dirname(destination), recursive = TRUE, showWarnings = FALSE)

record <- measure_record(
  samples = tibble::tibble(sample_id = "sample-dad"),
  injections = tibble::tibble(
    injection_id = "injection-dad",
    sample_id = "sample-dad"
  ),
  channels = tibble::tibble(
    channel_id = "dad",
    channel_name = "Diode-array detector"
  ),
  sources = tibble::tibble(
    source_id = "source-dad",
    source_name = "synthetic-dad.csv",
    source_sha256 = paste(rep("d", 64L), collapse = "")
  )
)

streams <- tibble::tibble(
  stream_id = "array2d",
  injection_id = "injection-dad",
  channel_id = "dad",
  kind = "nd",
  source_id = "source-dad",
  value_name = "absorbance",
  value_unit = "mAU"
)

# The wavelength spacing is irregular (250, 260, 280 nm), and (0, 260) is an
# absent cell. The remaining physical rows intentionally retain their native
# order rather than being sorted into a rectangular grid.
array_measurement <- new_measure_nd_tbl(
  location_1 = c(0, 0, 1, 1, 1, 2, 2, 2),
  location_2 = c(250, 280, 250, 260, 280, 250, 260, 280),
  value = c(10, 20, 30, NA_real_, 50, 60, 70, 80),
  dim_names = c("time", "wavelength"),
  dim_units = c("min", "nm")
)

arrays <- tibble::tibble(
  array_id = "dad-1",
  stream_id = "array2d",
  .measures = list(array_measurement),
  .source_rows = list(c(101L, NA_integer_, 103L, 104L, 105L, 106L, 107L, 108L)),
  .saturated = list(c(NA, FALSE, FALSE, FALSE, TRUE, FALSE, NA, FALSE))
)

acquisition <- measure_acquisition(
  record = record,
  streams = streams,
  arrays = arrays
)
write_measure_acquisition(acquisition, destination)
message("Wrote ", normalizePath(destination, mustWork = FALSE))
