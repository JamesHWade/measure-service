# Fit the declared ALS study through the public measure recipe step.
suppressPackageStartupMessages({
  library(jsonlite)
  library(recipes)
  library(tibble)
})

request <- fromJSON(
  paste(readLines(file("stdin"), warn = FALSE), collapse = "\n"),
  simplifyVector = FALSE
)

as_num <- function(x) as.numeric(unlist(x, use.names = FALSE))
windows <- lapply(request$low_peak_windows, as_num)
lambda_values <- as_num(request$lambda_values)

measure_package <- find.package("measure")
measure_rdb <- file.path(measure_package, "R", "measure.rdb")
measure_rdb_md5 <- if (file.exists(measure_rdb)) {
  unname(as.character(tools::md5sum(measure_rdb)))
} else {
  stop("The installed measure R code database was not found.")
}
runtime <- list(
  r_version = as.character(getRversion()),
  measure_version = as.character(utils::packageVersion("measure")),
  measure_r_code_database_md5 = measure_rdb_md5,
  recipes_version = as.character(utils::packageVersion("recipes")),
  library_paths = as.list(.libPaths())
)

fit_one <- function(input, lambda) {
  x <- as_num(input$axis)
  y <- as_num(input$observed_signal)
  truth <- as_num(input$true_baseline)
  if (!(length(x) == length(y) && length(y) == length(truth))) {
    stop("Study input axis, signal, and baseline lengths differ.")
  }
  if (!all(is.finite(x)) || !all(is.finite(y)) || !all(is.finite(truth))) {
    stop("Study inputs must contain only finite values.")
  }

  data <- tibble::tibble(input_id = "als-study")
  data$signal <- measure::new_measure_list(
    list(measure::new_measure_tbl(x, y))
  )
  recipe <- recipes::recipe(data)
  recipe <- measure::step_measure_baseline_als(
    recipe,
    measures = "signal",
    lambda = lambda,
    p = request$asymmetry_p,
    max_iter = request$max_iter
  )

  fitted <- purrr::quietly(function() {
    recipes::bake(recipes::prep(recipe), new_data = NULL)
  })()
  baked <- fitted$result
  fit_warnings <- fitted$warnings
  corrected <- baked$signal[[1]]$value
  estimated <- y - corrected
  error <- estimated - truth
  if (length(estimated) != length(y) || !all(is.finite(estimated))) {
    stop("ALS returned an invalid estimated baseline.")
  }

  low_peak <- rep(FALSE, length(x))
  for (window in windows) {
    low_peak <- low_peak | (x >= window[[1]] & x <= window[[2]])
  }
  left_endpoint <- x <= request$endpoint_cut
  right_endpoint <- x >= max(x) - request$endpoint_cut
  if (!any(low_peak) || !any(left_endpoint) || !any(right_endpoint)) {
    stop("A declared metric window contains no study samples.")
  }

  summarize_window <- function(mask, prefix) {
    list(
      setNames(list(mean(truth[mask])), paste0(prefix, "_true_mean")),
      setNames(list(mean(estimated[mask])), paste0(prefix, "_estimated_mean")),
      setNames(list(mean(error[mask])), paste0(prefix, "_mean_error")),
      setNames(list(sqrt(mean(error[mask]^2))), paste0(prefix, "_rmse"))
    )
  }

  values <- list(
    input_id = input$input_id,
    input_sha256 = input$input_sha256,
    seed = as.integer(input$seed),
    sample_count = length(x),
    noise_sd = as.numeric(input$noise_sd),
    lambda = as.numeric(lambda),
    asymmetry_p = as.numeric(request$asymmetry_p),
    max_iter = as.integer(request$max_iter),
    baseline_rmse_overall = sqrt(mean(error^2)),
    baseline_rmse_peak_free = sqrt(mean(error[low_peak]^2)),
    endpoint_cut = as.numeric(request$endpoint_cut),
    fit_warnings = paste(unique(fit_warnings), collapse = " | ")
  )
  values <- c(
    values,
    unlist(summarize_window(left_endpoint, "endpoint_left"), recursive = FALSE),
    unlist(
      summarize_window(right_endpoint, "endpoint_right"),
      recursive = FALSE
    )
  )
  if (
    !all(
      vapply(
        values[setdiff(
          names(values),
          c("input_id", "input_sha256", "fit_warnings")
        )],
        function(value) {
          is.numeric(value) && length(value) == 1L && is.finite(value)
        },
        logical(1)
      )
    )
  ) {
    stop("ALS fit metrics must be finite scalar numbers.")
  }
  values
}

fits <- list()
for (input in request$inputs) {
  for (lambda in lambda_values) {
    fits[[length(fits) + 1L]] <- fit_one(input, lambda)
  }
}

cat(
  jsonlite::toJSON(
    list(runtime = runtime, fits = fits),
    auto_unbox = TRUE,
    null = "null",
    digits = 17
  ),
  "\n"
)
