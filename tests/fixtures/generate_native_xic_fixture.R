#!/usr/bin/env Rscript

# Regenerate the compact native acquisition fixture used by worker tests.
# The default destination is checked in; pass another directory while
# developing because the native writer never replaces an existing path.
suppressPackageStartupMessages(library(measure))

arguments <- commandArgs(trailingOnly = TRUE)
destination <- if (length(arguments) == 0L) {
  file.path("tests", "fixtures", "native_acquisition_xic_v1")
} else {
  arguments[[1L]]
}

if (file.exists(destination)) {
  stop("Refusing to replace an existing fixture destination: ", destination)
}
dir.create(dirname(destination), recursive = TRUE, showWarnings = FALSE)

record <- measure_record(
  samples = tibble::tibble(sample_id = "sample-xic"),
  injections = tibble::tibble(
    injection_id = "injection-xic",
    sample_id = "sample-xic"
  ),
  channels = tibble::tibble(
    channel_id = "ms",
    channel_name = "MS centroid"
  ),
  sources = tibble::tibble(
    source_id = "source-native",
    source_name = "synthetic-native.csv",
    source_sha256 = paste(rep("b", 64L), collapse = "")
  )
)

streams <- tibble::tibble(
  stream_id = "ms-xic",
  injection_id = "injection-xic",
  channel_id = "ms",
  kind = "ms",
  source_id = "source-native",
  time_unit = "s",
  mz_unit = "m/z",
  value_name = "intensity",
  value_unit = "counts"
)

scan_measurements <- list(
  new_measure_tbl(c(99, 100, 101, 105), c(1, 2, 3, 4)),
  new_measure_tbl(),
  new_measure_tbl(),
  new_measure_tbl(c(100, 101), c(4, 6)),
  new_measure_tbl(c(100, 102), c(7, 8))
)

scans <- tibble::tibble(
  scan_id = c(
    "scan-1",
    "scan-missing",
    "scan-empty",
    "scan-saturated",
    "scan-5"
  ),
  stream_id = rep("ms-xic", 5L),
  scan_index = 1:5,
  scan_status = c("acquired", "missing", "acquired", "acquired", "acquired"),
  retention_time = c(0.5, 1, 1.5, 2, 2.5),
  polarity = rep("positive", 5L),
  ms_level = rep(1L, 5L),
  spectrum_type = c(
    "centroid",
    NA_character_,
    "centroid",
    "centroid",
    "centroid"
  ),
  source_scan_id = paste0("raw-00", 1:5),
  .measures = scan_measurements,
  .source_rows = list(
    c(10L, 11L, 12L, 13L),
    integer(),
    integer(),
    c(20L, 21L),
    c(30L, 31L)
  ),
  .saturated = list(
    c(FALSE, NA, FALSE, FALSE),
    logical(),
    logical(),
    c(TRUE, FALSE),
    c(FALSE, NA)
  )
)

acquisition <- measure_acquisition(
  record = record,
  streams = streams,
  scans = scans
)
write_measure_acquisition(acquisition, destination)
message("Wrote ", normalizePath(destination, mustWork = FALSE))
