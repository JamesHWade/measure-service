# A constrained R worker for the separations MCP prototype. JSON is exchanged
# over stdin/stdout; neither file paths nor R expressions come from the caller.
suppressPackageStartupMessages(library(jsonlite))

input <- fromJSON(
  paste(readLines(file("stdin"), warn = FALSE), collapse = "\n"),
  simplifyVector = FALSE
)
warning_state <- new.env(parent = emptyenv())
warning_state$seen <- character()

package_versions <- function() {
  names <- c("measure", "measure.sec", "recipes", "Matrix")
  stats::setNames(
    lapply(names, function(name) {
      if (requireNamespace(name, quietly = TRUE)) {
        as.character(utils::packageVersion(name))
      } else {
        NULL
      }
    }),
    names
  )
}

# A package version alone does not distinguish an unreleased development
# checkout from another build with the same DESCRIPTION version.
package_code_fingerprints <- function() {
  names <- c("measure", "measure.sec")
  stats::setNames(
    lapply(names, function(name) {
      if (!requireNamespace(name, quietly = TRUE)) {
        return(NULL)
      }
      code_file <- file.path(
        system.file(package = name),
        "R",
        paste0(name, ".rdb")
      )
      if (!file.exists(code_file)) {
        return(NULL)
      }
      unname(as.character(tools::md5sum(code_file)))
    }),
    names
  )
}

as_num <- function(x) as.numeric(unlist(x, use.names = FALSE))

# Native acquisition inputs are passed as exact UTF-8 members rather than
# paths. The R package reader remains the authority for the bundle format;
# this adapter only bounds the transport and writes the already-validated
# member bytes to a private temporary directory.
native_bundle_files <- list(
  csv = c(
    "manifest.dcf",
    "samples.csv",
    "injections.csv",
    "channels.csv",
    "sources.csv",
    "curves.csv",
    "points.csv",
    "streams.csv",
    "scans.csv",
    "scan_points.csv",
    "arrays.csv",
    "array_axes.csv",
    "array_points.csv"
  ),
  tsv = c(
    "manifest.dcf",
    "samples.tsv",
    "injections.tsv",
    "channels.tsv",
    "sources.tsv",
    "curves.tsv",
    "points.tsv",
    "streams.tsv",
    "scans.tsv",
    "scan_points.tsv",
    "arrays.tsv",
    "array_axes.tsv",
    "array_points.tsv"
  )
)
native_bundle_max_bytes <- 4000000L
native_bundle_max_file_bytes <- native_bundle_max_bytes
native_trace_max_rows <- 50000L
native_trace_max_points <- 100000L
native_dad_max_points <- 4096L
native_array_identity_max_items <- 128L
native_array_identity_max_bytes <- 48L * 1024L

as_native_text <- function(x, field, max_bytes = NULL) {
  if (
    !is.character(x) ||
      length(x) != 1L ||
      is.na(x) ||
      Encoding(x) == "bytes" ||
      !validUTF8(x)
  ) {
    stop(field, " must be one nonmissing UTF-8 string.")
  }
  bytes <- charToRaw(x)
  if (any(as.integer(bytes) == 0L)) {
    stop(field, " must not contain NUL bytes.")
  }
  if (!is.null(max_bytes) && length(bytes) > max_bytes) {
    stop(field, " exceeds the per-file byte limit.")
  }
  x
}

native_bundle_input <- function(input) {
  files <- input$files
  if (
    !is.list(files) ||
      is.data.frame(files) ||
      is.null(names(files)) ||
      anyNA(names(files)) ||
      anyDuplicated(names(files))
  ) {
    stop("files must be a named object of bundle members.")
  }
  matching <- Filter(
    function(nameset) setequal(names(files), nameset),
    native_bundle_files
  )
  if (length(matching) != 1L) {
    stop(
      "files must contain exactly the fixed CSV or TSV bundle members."
    )
  }
  ordered_names <- matching[[1L]]
  sizes <- vapply(
    ordered_names,
    function(name) {
      value <- as_native_text(files[[name]], paste0("files.", name))
      length(charToRaw(value))
    },
    integer(1)
  )
  if (any(sizes > native_bundle_max_file_bytes)) {
    stop("A native acquisition bundle member exceeds the per-file byte limit.")
  }
  total <- sum(sizes)
  if (!is.finite(total) || total > native_bundle_max_bytes) {
    stop("The native acquisition bundle exceeds the cumulative byte limit.")
  }
  list(
    files = files,
    names = ordered_names,
    bytes = sizes,
    total_bytes = total
  )
}

with_native_acquisition <- function(input, callback) {
  bundle <- native_bundle_input(input)
  directory <- tempfile("measure-native-acquisition-")
  dir.create(directory, mode = "0700")
  on.exit(unlink(directory, recursive = TRUE, force = TRUE), add = TRUE)
  for (name in bundle$names) {
    connection <- file(file.path(directory, name), open = "wb")
    writeBin(charToRaw(bundle$files[[name]]), connection)
    close(connection)
  }
  acquisition <- measure::read_measure_acquisition(
    directory,
    max_bytes = native_bundle_max_bytes
  )
  callback(acquisition, bundle)
}

native_nullable <- function(x) {
  if (length(x) == 0L || is.na(x[[1L]])) {
    NULL
  } else {
    unname(x[[1L]])
  }
}

native_context_rows <- function(acquisition) {
  record <- acquisition$record
  list(
    samples = lapply(record$samples$sample_id, identity),
    injections = lapply(seq_len(nrow(record$injections)), function(i) {
      list(
        injection_id = record$injections$injection_id[[i]],
        sample_id = record$injections$sample_id[[i]]
      )
    }),
    channels = lapply(seq_len(nrow(record$channels)), function(i) {
      list(
        channel_id = record$channels$channel_id[[i]],
        channel_name = native_nullable(record$channels$channel_name[i])
      )
    }),
    sources = lapply(seq_len(nrow(record$sources)), function(i) {
      list(
        source_id = record$sources$source_id[[i]],
        source_name = native_nullable(record$sources$source_name[i]),
        source_sha256 = native_nullable(record$sources$source_sha256[i])
      )
    })
  )
}

native_stream_rows <- function(acquisition) {
  streams <- acquisition$streams
  lapply(seq_len(nrow(streams)), function(i) {
    list(
      stream_id = streams$stream_id[[i]],
      injection_id = streams$injection_id[[i]],
      channel_id = streams$channel_id[[i]],
      kind = streams$kind[[i]],
      source_id = native_nullable(streams$source_id[i]),
      time_unit = native_nullable(streams$time_unit[i]),
      mz_unit = native_nullable(streams$mz_unit[i]),
      value_name = native_nullable(streams$value_name[i]),
      value_unit = native_nullable(streams$value_unit[i])
    )
  })
}

native_array_identity_rows <- function(acquisition) {
  arrays <- acquisition$arrays
  count <- nrow(arrays)
  if (count == 0L) {
    return(list())
  }
  limit <- min(count, native_array_identity_max_items)
  rows <- list()
  for (i in seq_len(limit)) {
    candidate <- list(
      array_id = arrays$array_id[[i]],
      stream_id = arrays$stream_id[[i]],
      point_count = as.integer(nrow(arrays$.measures[[i]]))
    )
    trial <- c(rows, list(candidate))
    serialized <- jsonlite::toJSON(trial, auto_unbox = TRUE, null = "null")
    if (length(charToRaw(serialized)) > native_array_identity_max_bytes) {
      break
    }
    rows <- trial
  }
  rows
}

native_status_counts <- function(values) {
  counts <- table(values, useNA = "no")
  as.list(stats::setNames(as.integer(counts), names(counts)))
}

native_inspection <- function(acquisition, bundle) {
  scan_points <- sum(vapply(acquisition$scans$.measures, nrow, integer(1)))
  array_points <- sum(vapply(acquisition$arrays$.measures, nrow, integer(1)))
  record_counts <- summary(acquisition)$counts
  context <- native_context_rows(acquisition)
  counts <- c(
    as.list(record_counts),
    list(
      scan_points = scan_points,
      array_points = array_points
    )
  )
  inspection_summary <- list(
    counts = counts,
    scan_status_counts = native_status_counts(acquisition$scans$scan_status),
    streams = native_stream_rows(acquisition),
    samples = context$samples,
    injections = context$injections,
    channels = context$channels,
    sources = context$sources
  )
  if (nrow(acquisition$arrays) > 0L) {
    inspection_summary$arrays <- native_array_identity_rows(acquisition)
  }
  list(
    profile = "measure-acquisition-inspection/1",
    summary = inspection_summary,
    provenance = list(
      operation = "inspect_acquisition",
      backend = "measure",
      package = as.character(utils::packageVersion("measure")),
      bundle_bytes = bundle$total_bytes,
      source_ids = acquisition$record$sources$source_id,
      source_sha256 = acquisition$record$sources$source_sha256,
      packages = package_versions(),
      installed_r_code_md5 = package_code_fingerprints(),
      r_version = as.character(getRversion())
    )
  )
}

native_dad_selection <- function(input) {
  array_id <- as_native_text(input$array_id, "array_id")
  time_axis <- input$time_axis
  if (
    is.logical(time_axis) ||
      !is.numeric(time_axis) ||
      length(time_axis) != 1L ||
      !is.finite(time_axis) ||
      time_axis != floor(time_axis) ||
      !time_axis %in% c(1, 2)
  ) {
    stop("time_axis must be exactly 1 or 2.")
  }
  native_dad_range <- function(value, field, positive = FALSE) {
    if (is.null(value)) {
      return(NULL)
    }
    values <- if (
      is.list(value) &&
        is.null(names(value)) &&
        length(value) == 2L &&
        all(
          vapply(
            value,
            function(item) {
              is.numeric(item) &&
                !is.logical(item) &&
                length(item) == 1L
            },
            logical(1)
          )
        )
    ) {
      vapply(value, as.numeric, numeric(1))
    } else {
      NULL
    }
    if (
      is.null(values) ||
        is.logical(values) ||
        !is.numeric(values) ||
        length(values) != 2L ||
        !all(is.finite(values)) ||
        values[[1L]] > values[[2L]] ||
        (positive && any(values <= 0))
    ) {
      stop(
        field,
        " must be two finite, ordered numbers",
        if (positive) " with positive endpoints" else "",
        "."
      )
    }
    as.numeric(values)
  }
  time_range <- native_dad_range(input$time_range, "time_range")
  wavelength_range <- native_dad_range(
    input$wavelength_range,
    "wavelength_range",
    positive = TRUE
  )
  list(
    array_id = array_id,
    time_axis = as.integer(time_axis),
    time_range = time_range,
    wavelength_range = wavelength_range,
    has_window = !is.null(time_range) || !is.null(wavelength_range)
  )
}

native_dad_unit <- function(units, index, field) {
  if (
    is.null(units) ||
      length(units) != 2L ||
      is.na(units[[index]]) ||
      !nzchar(trimws(units[[index]], whitespace = "[\\h\\v]"))
  ) {
    stop(field, " must be a known nonblank unit for both array axes.")
  }
  unname(units[[index]])
}

native_dad_plane <- function(acquisition, selection) {
  arrays <- acquisition$arrays
  array_position <- match(selection$array_id, arrays$array_id)
  if (is.na(array_position)) {
    stop("The requested array_id was not found in the acquisition.")
  }
  stream_id <- arrays$stream_id[[array_position]]
  stream_position <- match(stream_id, acquisition$streams$stream_id)
  if (is.na(stream_position)) {
    stop("The selected array refers to an unknown stream.")
  }

  measure <- arrays$.measures[[array_position]]
  point_count <- nrow(measure)
  source_rows <- arrays$.source_rows[[array_position]]
  saturated <- arrays$.saturated[[array_position]]
  if (
    length(source_rows) != point_count ||
      length(saturated) != point_count
  ) {
    stop("The selected array lineage and saturation masks do not align.")
  }

  time_axis <- selection$time_axis
  wavelength_axis <- 3L - time_axis
  dim_units <- attr(measure, "dim_units", exact = TRUE)
  time_unit <- native_dad_unit(dim_units, time_axis, "time_unit")
  wavelength_unit <- native_dad_unit(
    dim_units,
    wavelength_axis,
    "wavelength_unit"
  )
  time <- as.numeric(measure[[paste0("location_", time_axis)]])
  wavelength <- as.numeric(
    measure[[paste0("location_", wavelength_axis)]]
  )
  if (!all(is.finite(time)) || !all(is.finite(wavelength))) {
    stop("DAD time and wavelength coordinates must be finite.")
  }
  if (any(wavelength <= 0)) {
    stop("DAD wavelengths must be positive.")
  }
  if (anyDuplicated(data.frame(time = time, wavelength = wavelength))) {
    stop("DAD time and wavelength coordinate pairs must be unique.")
  }

  value <- as.numeric(measure$value)
  if (any(is.nan(value)) || any(!is.na(value) & !is.finite(value))) {
    stop("DAD values must be finite or NA.")
  }
  selected <- rep(TRUE, point_count)
  if (!is.null(selection$time_range)) {
    selected <- selected &
      time >= selection$time_range[[1L]] &
      time <= selection$time_range[[2L]]
  }
  if (!is.null(selection$wavelength_range)) {
    selected <- selected &
      wavelength >= selection$wavelength_range[[1L]] &
      wavelength <= selection$wavelength_range[[2L]]
  }
  selected_indices <- which(selected)
  selected_count <- length(selected_indices)
  if (selected_count > native_dad_max_points) {
    if (selection$has_window) {
      stop("The selected DAD window exceeds the 4096 observed-point limit.")
    }
    stop("The selected DAD array exceeds the 4096 observed-point limit.")
  }
  points <- lapply(selected_indices, function(i) {
    list(
      point_index = as.integer(i),
      time = time[[i]],
      wavelength = wavelength[[i]],
      value = native_nullable(value[i]),
      source_row = native_nullable(source_rows[i]),
      saturated = native_nullable(saturated[i])
    )
  })

  stream <- acquisition$streams[stream_position, , drop = FALSE]
  result <- list(
    array_id = selection$array_id,
    stream_id = stream$stream_id[[1L]],
    injection_id = stream$injection_id[[1L]],
    channel_id = stream$channel_id[[1L]],
    source_id = native_nullable(stream$source_id[1L]),
    time_unit = time_unit,
    wavelength_unit = wavelength_unit,
    value_unit = native_nullable(stream$value_unit[1L]),
    points = points,
    packages = package_versions(),
    installed_r_code_md5 = package_code_fingerprints(),
    r_version = as.character(getRversion())
  )
  if (selection$has_window) {
    result$native_point_count <- as.integer(point_count)
  }
  result
}

native_trace_scalar <- function(x, field, minimum = -Inf) {
  values <- suppressWarnings(as_num(x))
  if (
    length(values) != 1L ||
      !is.finite(values[[1L]]) ||
      values[[1L]] < minimum ||
      values[[1L]] != floor(values[[1L]])
  ) {
    stop(field, " must be one finite whole-number scalar.")
  }
  as.integer(values[[1L]])
}

native_trace_selection <- function(input) {
  stream_id <- as_native_text(input$stream_id, "stream_id")
  polarity <- as_native_text(input$polarity, "polarity")
  if (!polarity %in% c("positive", "negative")) {
    stop("polarity must be exactly 'positive' or 'negative'.")
  }
  ms_level <- native_trace_scalar(input$ms_level, "ms_level", minimum = 1)
  mz_min <- input$mz_min
  mz_max <- input$mz_max
  if (is.null(mz_min) && is.null(mz_max)) {
    mz_range <- NULL
  } else {
    if (is.null(mz_min) || is.null(mz_max)) {
      stop("mz_min and mz_max must both be supplied for an XIC.")
    }
    mz_min <- suppressWarnings(as_num(mz_min))
    mz_max <- suppressWarnings(as_num(mz_max))
    if (
      length(mz_min) != 1L ||
        length(mz_max) != 1L ||
        !is.finite(mz_min) ||
        !is.finite(mz_max) ||
        mz_min <= 0 ||
        mz_max <= 0 ||
        mz_min > mz_max
    ) {
      stop(
        "mz_min and mz_max must be finite positive scalars with mz_min <= mz_max."
      )
    }
    mz_range <- c(as.numeric(mz_min), as.numeric(mz_max))
  }
  list(
    stream_id = stream_id,
    polarity = polarity,
    ms_level = ms_level,
    mz_range = mz_range
  )
}

native_trace_rows <- function(acquisition, trace) {
  scans <- acquisition$scans
  points <- vector("list", nrow(trace))
  source_rows <- vector("list", sum(trace$selected_points))
  next_source <- 0L
  scan_positions <- match(trace$scan_id, scans$scan_id)
  for (i in seq_len(nrow(trace))) {
    row <- trace[i, , drop = FALSE]
    scan_id <- row$scan_id[[1L]]
    scan_position <- scan_positions[[i]]
    if (is.na(scan_position)) {
      stop("The trace returned a scan that is absent from the acquisition.")
    }
    measures <- scans$.measures[[scan_position]]
    raw_source_rows <- scans$.source_rows[[scan_position]]
    raw_saturated <- scans$.saturated[[scan_position]]
    point_indices <- row$.point_indices[[1L]]
    if (is.null(point_indices)) {
      point_indices <- integer()
    } else {
      point_indices <- as.integer(point_indices)
    }
    if (length(point_indices) > 0L) {
      for (j in seq_along(point_indices)) {
        point_index <- point_indices[[j]]
        if (
          point_index < 1L ||
            point_index > nrow(measures) ||
            length(raw_source_rows) != nrow(measures) ||
            length(raw_saturated) != nrow(measures)
        ) {
          stop("The trace returned a point index outside its source scan.")
        }
        next_source <- next_source + 1L
        source_rows[[next_source]] <- list(
          scan_id = scan_id,
          point_index = point_index,
          source_row = native_nullable(raw_source_rows[point_index]),
          mz = as.numeric(measures$location[[point_index]]),
          intensity = native_nullable(measures$value[point_index]),
          saturated = native_nullable(raw_saturated[point_index])
        )
      }
    }
    points[[i]] <- list(
      scan_id = scan_id,
      scan_index = as.integer(row$scan_index[[1L]]),
      source_scan_id = native_nullable(row$source_scan_id[1L]),
      retention_time = as.numeric(row$retention_time[[1L]]),
      value = native_nullable(row$value[1L]),
      status = row$status[[1L]],
      selected_points = as.integer(row$selected_points[[1L]]),
      saturation_unknown = as.logical(row$saturation_unknown[[1L]])
    )
  }
  list(points = points, source_rows = source_rows)
}

native_trace_extraction <- function(acquisition, selection) {
  trace <- measure::measure_ms_trace(
    acquisition,
    stream_id = selection$stream_id,
    polarity = selection$polarity,
    ms_level = selection$ms_level,
    mz_range = selection$mz_range
  )
  if (nrow(trace) > native_trace_max_rows) {
    stop("The extracted trace exceeds the scan-row limit.")
  }
  selected_count <- sum(as_num(trace$selected_points))
  if (!is.finite(selected_count) || selected_count > native_trace_max_points) {
    stop("The extracted trace exceeds the selected-point limit.")
  }
  rows <- native_trace_rows(acquisition, trace)
  if (length(rows$source_rows) > native_trace_max_points) {
    stop("The extracted trace exceeds the source-row limit.")
  }
  processing <- attr(trace, "processing")
  if (!is.list(processing)) {
    stop("The measure backend returned no processing metadata.")
  }
  list(
    points = rows$points,
    source_rows = rows$source_rows,
    processing = processing,
    packages = package_versions(),
    installed_r_code_md5 = package_code_fingerprints(),
    r_version = as.character(getRversion())
  )
}

project_peak_rows <- function(value) {
  required <- c(
    "peak_id",
    "location",
    "height",
    "left_base",
    "right_base",
    "area"
  )
  optional <- c("area_basis", "prominence")
  peaks <- as.data.frame(value)
  if (nrow(peaks) == 0L) {
    return(list())
  }
  missing <- setdiff(required, names(peaks))
  if (length(missing) > 0L) {
    stop("The peak backend returned a malformed nonempty peak table.")
  }
  peaks[, c(required, intersect(optional, names(peaks))), drop = FALSE]
}

peak_ranges_overlap <- function(peaks) {
  if (!is.data.frame(peaks) || nrow(peaks) < 2L) {
    return(FALSE)
  }
  left <- suppressWarnings(as_num(peaks$left_base))
  right <- suppressWarnings(as_num(peaks$right_base))
  finite <- is.finite(left) & is.finite(right) & right > left
  if (sum(finite) < 2L) {
    return(FALSE)
  }
  order_index <- order(left[finite], right[finite])
  left <- left[finite][order_index]
  right <- right[finite][order_index]
  furthest_right <- right[[1]]
  for (index in seq.int(2L, length(left))) {
    if (left[[index]] < furthest_right) {
      return(TRUE)
    }
    furthest_right <- max(furthest_right, right[[index]])
  }
  FALSE
}

analyze <- function(input) {
  if (identical(input$operation, "capabilities")) {
    return(list(
      r_version = as.character(getRversion()),
      packages = package_versions(),
      installed_r_code_md5 = package_code_fingerprints(),
      sec_calibrated_curve_support = requireNamespace(
        "measure.sec",
        quietly = TRUE
      ) &&
        all(
          c("mw_col", "mw_scale") %in%
            names(formals(measure.sec::step_sec_mw_averages))
        ) &&
        all(
          c("mw_col", "mw_scale", "integration_range") %in%
            names(formals(measure.sec::step_sec_mw_distribution))
        ),
      native_acquisition_support = requireNamespace(
        "measure",
        quietly = TRUE
      ) &&
        all(
          c("read_measure_acquisition", "measure_acquisition") %in%
            getNamespaceExports("measure")
        ),
      centroid_xic_support = requireNamespace("measure", quietly = TRUE) &&
        "measure_ms_trace" %in% getNamespaceExports("measure")
    ))
  }
  if (
    !requireNamespace("measure", quietly = TRUE) ||
      !requireNamespace("recipes", quietly = TRUE)
  ) {
    stop("The measure and recipes R packages are required.")
  }

  if (identical(input$operation, "inspect_acquisition")) {
    return(with_native_acquisition(input, native_inspection))
  }

  if (identical(input$operation, "extract_ms_trace")) {
    selection <- native_trace_selection(input)
    return(
      with_native_acquisition(
        input,
        function(acquisition, ...) {
          native_trace_extraction(acquisition, selection)
        }
      )
    )
  }

  if (identical(input$operation, "read_dad_plane")) {
    selection <- native_dad_selection(input)
    return(
      with_native_acquisition(
        input,
        function(acquisition, ...) {
          native_dad_plane(acquisition, selection)
        }
      )
    )
  }

  if (identical(input$operation, "quantitation")) {
    method <- input$method
    standards <- data.frame(
      concentration = vapply(
        method$standards,
        function(s) as.numeric(s$concentration),
        numeric(1)
      ),
      response = vapply(
        method$standards,
        function(s) as.numeric(s$response),
        numeric(1)
      )
    )
    calibration <- measure::measure_calibration_fit(
      standards,
      response ~ concentration,
      model = "linear",
      weights = method$weighting,
      origin = method$force_origin
    )
    coefficients <- stats::coef(calibration$model)
    slope <- unname(coefficients["concentration"])
    if (!is.finite(slope) || slope <= 0) {
      stop("The fitted calibration slope must be positive and finite.")
    }
    predicted <- measure::measure_calibration_predict(
      calibration,
      data.frame(response = as.numeric(input$response))
    )$.pred_conc[[1]]
    if (!is.finite(predicted)) {
      stop("The calibration could not produce a finite concentration.")
    }
    limits <- range(standards$concentration)
    outside <- predicted < limits[1] || predicted > limits[2]
    if (outside) {
      warning(
        "Predicted concentration lies outside the supplied standard concentration range."
      )
    }
    intercept <- if ("(Intercept)" %in% names(coefficients)) {
      unname(coefficients["(Intercept)"])
    } else {
      0
    }
    return(list(
      kind = "peak_quantitation",
      summary = list(
        analyte = method$analyte,
        concentration = predicted,
        concentration_unit = method$concentration_unit,
        response = as.numeric(input$response),
        response_unit = method$response_unit,
        outside_standard_range = outside
      ),
      diagnostics = list(
        slope = slope,
        intercept = intercept,
        r_squared = calibration$diagnostics$r_squared,
        residual_standard_error = calibration$diagnostics$sigma,
        residual_degrees_of_freedom = calibration$model$df.residual,
        standard_range = limits,
        standard_results = data.frame(
          standards,
          fitted_response = as.numeric(stats::fitted(calibration$model))
        )
      ),
      notes = c(
        "The caller assigns the peak identity and declares that the standard and unknown response bases match.",
        "No internal-standard correction, dilution factor, detection limit, or uncertainty interval is applied."
      ),
      packages = package_versions(),
      installed_r_code_md5 = package_code_fingerprints(),
      r_version = as.character(getRversion())
    ))
  }

  x <- as_num(input$trace$axis)
  y <- as_num(input$trace$signal)
  dat <- tibble::tibble(trace = input$trace$trace_id)
  dat$signal <- measure::new_measure_list(list(measure::new_measure_tbl(x, y)))
  rec <- recipes::recipe(dat)

  if (identical(input$operation, "chromatogram")) {
    method <- input$method
    if (
      isTRUE(
        method$min_height == 0 &&
          method$min_prominence == 0 &&
          method$min_distance == 0
      )
    ) {
      warning(
        "Peak detection with all thresholds set to zero can retain noise; inspect the signal range and sampling before choosing thresholds."
      )
    }
    if (identical(method$baseline, "als")) {
      rec <- measure::step_measure_baseline_als(
        rec,
        measures = "signal",
        lambda = method$baseline_lambda,
        p = method$baseline_p
      )
    }
    if (identical(method$smoothing, "gaussian")) {
      rec <- measure::step_measure_smooth_gaussian(
        rec,
        measures = "signal",
        sigma = method$smoothing_sigma
      )
    }
    rec <- measure::step_measure_peaks_detect(
      rec,
      measures = "signal",
      algorithm = "prominence",
      min_height = method$min_height,
      min_prominence = method$min_prominence,
      min_distance = method$min_distance
    )
    rec <- measure::step_measure_peaks_integrate(
      rec,
      measures = "signal",
      method = "trapezoid",
      baseline = method$integration_baseline
    )
    out <- recipes::bake(recipes::prep(rec), new_data = NULL)
    peaks <- project_peak_rows(out$.peaks[[1]])
    if (peak_ranges_overlap(peaks)) {
      warning(
        "Peak integration ranges overlap in their interiors; stored peak areas are unchanged and shared signal may contribute to more than one area."
      )
    }
    return(list(
      kind = "chromatogram",
      peak_count = if (is.data.frame(peaks)) nrow(peaks) else 0L,
      peaks = peaks,
      processed_trace = list(
        axis = out$signal[[1]]$location,
        signal = out$signal[[1]]$value
      ),
      notes = c(
        "Peak areas use the specified baseline and trapezoidal integration; negative integrand values are clipped to zero.",
        "Peak area is detector response, not concentration or compound identity."
      ),
      packages = package_versions(),
      installed_r_code_md5 = package_code_fingerprints(),
      r_version = as.character(getRversion())
    ))
  }

  if (identical(input$operation, "sec")) {
    if (!requireNamespace("measure.sec", quietly = TRUE)) {
      stop("The measure.sec R package is required for SEC analysis.")
    }
    if (
      !all(
        c("mw_col", "mw_scale") %in%
          names(formals(measure.sec::step_sec_mw_averages))
      ) ||
        !all(
          c("mw_col", "mw_scale", "integration_range") %in%
            names(formals(measure.sec::step_sec_mw_distribution))
        )
    ) {
      stop(
        "The installed measure.sec lacks calibrated-curve support; install the corrected development revision."
      )
    }
    method <- input$method
    standards <- data.frame(
      location = vapply(
        method$standards,
        function(s) as.numeric(s$location),
        numeric(1)
      ),
      mw = vapply(
        method$standards,
        function(s) as.numeric(s$molecular_weight_g_mol),
        numeric(1)
      )
    )
    rec <- measure.sec::step_sec_conventional_cal(
      rec,
      measures = "signal",
      standards = standards,
      fit_type = method$fit_type,
      extrapolation = method$extrapolation,
      output_col = "calibrated_mw"
    )
    limits <- if (is.null(method$integration_range)) {
      NULL
    } else {
      as_num(method$integration_range)
    }
    selected <- if (is.null(limits)) {
      rep(TRUE, length(x))
    } else {
      x >= limits[1] & x <= limits[2]
    }
    if (sum(selected & y > 0) < 2) {
      stop(
        "The integration range must contain at least two positive signal points."
      )
    }
    rec <- measure.sec::step_sec_mw_averages(
      rec,
      measures = "signal",
      integration_range = limits
    )
    rec <- measure.sec::step_sec_mw_distribution(
      rec,
      measures = "signal",
      type = "both",
      n_points = 101L,
      integration_range = limits
    )
    trained <- recipes::prep(rec)
    out <- recipes::bake(trained, new_data = NULL)
    diag <- trained$steps[[1]]$calibration_diagnostics
    if (diag$degrees_of_freedom <= 0) {
      warning(
        "Calibration has no residual degrees of freedom; fit uncertainty cannot be estimated from these standards."
      )
    }
    differential <- out$signal_differential[[1]]
    cumulative <- out$signal_cumulative[[1]]
    return(list(
      kind = "sec",
      summary = list(
        mn_g_mol = out$mw_mn[[1]],
        mw_g_mol = out$mw_mw[[1]],
        mz_g_mol = out$mw_mz[[1]],
        mp_g_mol = out$mw_mp[[1]],
        dispersity = out$mw_dispersity[[1]]
      ),
      diagnostics = list(
        calibration_range = trained$steps[[1]]$calibration_range,
        r_squared = diag$r_squared,
        rmse_log_mw = diag$rmse_log_mw,
        max_abs_pct_deviation = diag$max_abs_pct_deviation,
        residual_degrees_of_freedom = diag$degrees_of_freedom,
        standard_results = as.data.frame(diag$standard_results)
      ),
      distribution = list(
        log10_mw = differential$location,
        differential = differential$value,
        cumulative = cumulative$value
      ),
      notes = c(
        "Relative SEC calibration depends on the standards and their polymer/solvent applicability.",
        "The supplied signal is assumed proportional to weight concentration and already baseline-corrected."
      ),
      packages = package_versions(),
      installed_r_code_md5 = package_code_fingerprints(),
      r_version = as.character(getRversion())
    ))
  }
  stop("Unsupported analysis operation.")
}

result <- tryCatch(
  withCallingHandlers(
    list(ok = TRUE, result = analyze(input)),
    warning = function(w) {
      warning_state$seen <- c(warning_state$seen, conditionMessage(w))
      invokeRestart("muffleWarning")
    }
  ),
  error = function(e) list(ok = FALSE, error = conditionMessage(e))
)
result$warnings <- unique(warning_state$seen)
cat(
  toJSON(result, auto_unbox = TRUE, null = "null", na = "null", digits = 16),
  "\n"
)
