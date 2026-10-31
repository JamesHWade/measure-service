# Plot the sealed fixed study without rerunning a simulator or optimizer.
main <- function(args) {
  if (length(args) != 1L) {
    stop("Usage: Rscript scripts/plot_synthetic_ablations.R /path/to/study")
  }
  for (package in c("ggplot2", "jsonlite", "digest")) {
    if (!requireNamespace(package, quietly = TRUE)) {
      stop("Install the plotting dependency: ", package)
    }
  }
  root <- normalizePath(args[[1L]], mustWork = TRUE)
  seal_path <- file.path(root, "complete.json")
  seal_digest <- digest::digest(file = seal_path, algo = "sha256")
  seal <- jsonlite::fromJSON(seal_path, simplifyVector = FALSE)
  required <- c(
    "report.json",
    "protocol.json",
    "evaluations.csv",
    "preflight.json",
    "success-trajectories.csv",
    "paired-comparisons.csv",
    "measure.sqlite3"
  )
  if (
    !identical(seal$schema_version, 1L) || !all(required %in% names(seal$files))
  ) {
    stop("The study completion manifest is incomplete")
  }
  for (name in names(seal$files)) {
    path <- file.path(root, name)
    expected <- seal$files[[name]]
    if (
      name != basename(name) ||
        !file.exists(path) ||
        nzchar(Sys.readlink(path)) ||
        isTRUE(file.info(path)$isdir) ||
        file.info(path)$size != expected$size_bytes ||
        digest::digest(file = path, algo = "sha256") != expected$sha256
    ) {
      stop("Study evidence failed integrity verification: ", name)
    }
  }
  report <- jsonlite::fromJSON(
    file.path(root, "report.json"),
    simplifyVector = FALSE
  )
  if (
    !identical(report$profile, "synthetic-isocratic-ablation-study/1") ||
      !identical(
        report$study_input$sha256,
        "0c607fa590c3c5682018765f25b37c4c9820a2ef36fc3d57bb205d0f1aa31094"
      )
  ) {
    stop("Unsupported study profile")
  }
  curves <- utils::read.csv(file.path(root, "success-trajectories.csv"))
  arms <- c(
    "random-control",
    "gp-utility-default",
    "gp-confirmed-default",
    "gp-utility-low-noise",
    "gp-confirmed-low-noise"
  )
  cohorts <- c("regression", "held_out", "combined")
  series <- c("strict_analytical_accepted", "finite_resolution_score_ge_2")
  keys <- with(curves, paste(arm_id, cohort_role, series, high_fidelity_budget))
  expected_keys <- expand.grid(
    arm = arms,
    cohort = cohorts,
    series = series,
    budget = 1:10
  )
  denominator <- ifelse(curves$cohort_role == "combined", 12L, 6L)
  targets <- stats::setNames(
    c(
      "historical_utility",
      "historical_utility",
      "confirmed_acceptance_reward",
      "historical_utility",
      "confirmed_acceptance_reward"
    ),
    arms
  )
  priors <- stats::setNames(
    c("default", "default", "default", "low_noise", "low_noise"),
    arms
  )
  count_columns <- c(
    "success_count",
    "unknown_success_count",
    "observed_no_success_count"
  )
  if (
    nrow(curves) != 300L ||
      anyDuplicated(keys) ||
      !setequal(
        keys,
        with(expected_keys, paste(arm, cohort, series, budget))
      ) ||
      any(curves$optimizer_target_config != unname(targets[curves$arm_id])) ||
      any(curves$surrogate_noise_prior != unname(priors[curves$arm_id])) ||
      !all(is.finite(as.matrix(curves[, count_columns]))) ||
      any(as.matrix(curves[, count_columns]) < 0) ||
      any(as.matrix(curves[, count_columns]) %% 1 != 0) ||
      any(curves$cohort_size != denominator) ||
      any(rowSums(curves[, count_columns]) != denominator) ||
      any(
        abs(curves$success_fraction - curves$success_count / denominator) >
          1e-12
      ) ||
      any(
        abs(
          curves$success_fraction_upper_bound -
            (curves$success_count + curves$unknown_success_count) / denominator
        ) >
          1e-12
      )
  ) {
    stop(
      "Trajectory rows do not preserve every declared arm, cohort and budget"
    )
  }
  data <- curves[curves$cohort_role != "combined", ]
  data$arm_id <- factor(data$arm_id, levels = arms)
  data$cohort_role <- factor(
    data$cohort_role,
    levels = c("regression", "held_out"),
    labels = c("Regression tasks", "Held-out tasks")
  )
  data$series <- factor(
    data$series,
    levels = series,
    labels = c("Strict analytical acceptance", "Historical utility at least 2")
  )
  palette <- stats::setNames(
    c("#555555", "#0072B2", "#D55E00", "#56B4E9", "#CC79A7"),
    arms
  )
  labels <- stats::setNames(
    c(
      "Random control",
      "GP: utility, reference prior",
      "GP: confirmation, reference prior",
      "GP: utility, low noise prior",
      "GP: confirmation, low noise prior"
    ),
    arms
  )
  line_types <- stats::setNames(
    c("dotted", "solid", "solid", "dashed", "dashed"),
    arms
  )
  point_shapes <- stats::setNames(c(15, 16, 17, 1, 2), arms)
  percent <- function(x) paste0(round(x * 100), "%")
  chart_theme <- ggplot2::theme_minimal(base_size = 11, base_family = "sans") +
    ggplot2::theme(
      legend.position = "top",
      legend.title = ggplot2::element_blank(),
      legend.text = ggplot2::element_text(size = 9),
      panel.grid.minor = ggplot2::element_blank(),
      strip.text = ggplot2::element_text(face = "bold"),
      axis.line = ggplot2::element_line(colour = "#555555", linewidth = 0.2),
      plot.title = ggplot2::element_text(face = "bold", size = 15),
      plot.subtitle = ggplot2::element_text(size = 10),
      plot.caption = ggplot2::element_text(hjust = 0, size = 9),
      plot.margin = ggplot2::margin(12, 18, 12, 12)
    )
  execution <- gsub("_", " ", report$status)
  success <- ggplot2::ggplot(
    data,
    ggplot2::aes(
      x = high_fidelity_budget,
      y = success_fraction,
      colour = arm_id,
      linetype = arm_id,
      shape = arm_id,
      group = arm_id
    )
  ) +
    ggplot2::geom_vline(
      xintercept = 3.5,
      linetype = "dotted",
      colour = "#888888"
    ) +
    ggplot2::geom_linerange(
      ggplot2::aes(
        ymin = success_fraction,
        ymax = success_fraction_upper_bound
      ),
      linewidth = 1.2,
      alpha = 0.18,
      show.legend = FALSE
    ) +
    ggplot2::geom_step(linewidth = 0.75) +
    ggplot2::geom_point(size = 1.7) +
    ggplot2::facet_grid(cohort_role ~ series) +
    ggplot2::scale_colour_manual(values = palette, labels = labels) +
    ggplot2::scale_linetype_manual(values = line_types, labels = labels) +
    ggplot2::scale_shape_manual(values = point_shapes, labels = labels) +
    ggplot2::scale_x_continuous(breaks = 1:10, limits = c(1, 10)) +
    ggplot2::scale_y_continuous(
      breaks = seq(0, 1, 1 / 3),
      limits = c(0, 1),
      labels = percent
    ) +
    ggplot2::guides(colour = ggplot2::guide_legend(nrow = 3, byrow = TRUE)) +
    ggplot2::labs(
      title = "Success trajectories in the fixed target and prior comparison",
      subtitle = paste0(
        "Six task/seed campaigns per arm in each cohort. Execution: ",
        execution
      ),
      x = "Synthetic evaluations, including three common initial runs",
      y = "Fraction of all planned campaigns",
      caption = paste(
        "Faint ranges retain unresolved outcomes; they are not confidence intervals. Initial coordinates are shared; each arm counts its own runs.",
        "The vertical dotted line ends initialization. Each cohort has two constructed tasks and three seeds; no population ranking is inferred.",
        sep = "\n"
      )
    ) +
    chart_theme
  endpoint <- data[data$high_fidelity_budget == 10L, ]
  endpoint$label <- paste0(
    endpoint$success_count,
    " / ",
    endpoint$cohort_size,
    ifelse(
      endpoint$unknown_success_count > 0,
      paste0("\n+", endpoint$unknown_success_count, " unresolved"),
      ""
    )
  )
  short_labels <- stats::setNames(
    c(
      "Random",
      "Utility\nreference",
      "Confirmation\nreference",
      "Utility\nlow noise",
      "Confirmation\nlow noise"
    ),
    arms
  )
  endpoints <- ggplot2::ggplot(
    endpoint,
    ggplot2::aes(x = arm_id, fill = arm_id)
  ) +
    ggplot2::geom_col(
      ggplot2::aes(y = success_fraction_upper_bound),
      width = 0.72,
      alpha = 0.22
    ) +
    ggplot2::geom_col(
      ggplot2::aes(y = success_fraction),
      width = 0.72,
      alpha = 0.95
    ) +
    ggplot2::geom_text(
      ggplot2::aes(y = success_fraction_upper_bound + 0.045, label = label),
      size = 3.0,
      vjust = 0,
      lineheight = 1.0
    ) +
    ggplot2::facet_grid(cohort_role ~ series) +
    ggplot2::scale_fill_manual(values = palette, guide = "none") +
    ggplot2::scale_x_discrete(labels = short_labels) +
    ggplot2::scale_y_continuous(
      breaks = seq(0, 1, 1 / 3),
      limits = c(0, 1.22),
      labels = percent
    ) +
    ggplot2::labs(
      title = "Confirmed and unresolved outcomes at the ten-evaluation budget",
      subtitle = paste0(
        "One shared random control and the four target/prior GP arms. Execution: ",
        execution
      ),
      x = "GP target and inferred-noise prior; random has no GP factors",
      y = "Fraction of all planned campaigns",
      caption = paste(
        "Solid bars show confirmed success; pale portions add unresolved outcomes. Labels show confirmed / planned and any unresolved count.",
        "Reference prior: Gamma(1.1, 0.05); low noise prior: Gamma(1.1, 100). Both infer noise; these are declared regularizers, not measured uncertainty.",
        sep = "\n"
      )
    ) +
    chart_theme
  plots <- list(success = success, endpoints = endpoints)
  destinations <- unlist(lapply(names(plots), function(name) {
    file.path(root, paste0("ablation-", name, c(".png", ".svg")))
  }))
  occupied <- function(paths) {
    links <- Sys.readlink(paths)
    any(file.exists(paths) | (!is.na(links) & nzchar(links)))
  }
  if (occupied(destinations)) {
    stop("Refusing to overwrite existing plots")
  }
  stage <- tempfile(".ablation-figures-", tmpdir = root)
  if (!dir.create(stage)) {
    stop("Could not stage study figures")
  }
  published <- character()
  publication_complete <- FALSE
  on.exit(
    {
      if (!publication_complete && length(published)) {
        unlink(published)
      }
      unlink(stage, recursive = TRUE)
    },
    add = TRUE
  )
  checked_device <- function(open_device) {
    force(open_device)
    function(...) {
      previous_device <- grDevices::dev.cur()
      open_device(...)
      if (identical(grDevices::dev.cur(), previous_device)) {
        stop(
          "The requested graphics device did not open; no figures were published"
        )
      }
    }
  }
  png_device <- function(filename, width, height, bg, ...) {
    grDevices::png(
      filename = filename,
      width = width,
      height = height,
      units = "in",
      res = 170,
      bg = bg,
      ...
    )
  }
  for (name in names(plots)) {
    for (extension in c("png", "svg")) {
      ggplot2::ggsave(
        file.path(stage, paste0("ablation-", name, ".", extension)),
        plots[[name]],
        width = 12,
        height = 8.5,
        dpi = 170,
        bg = "white",
        device = checked_device(
          if (extension == "svg") grDevices::svg else png_device
        )
      )
    }
  }
  if (digest::digest(file = seal_path, algo = "sha256") != seal_digest) {
    stop("The study manifest changed during plotting")
  }
  staged_files <- file.path(stage, basename(destinations))
  staged_info <- file.info(staged_files)
  if (
    !all(file.exists(staged_files)) ||
      !all(is.finite(staged_info$size)) ||
      any(staged_info$size <= 0) ||
      any(staged_info$isdir)
  ) {
    stop("Not every staged PNG/SVG was created; no figures were published")
  }
  if (occupied(destinations)) {
    stop("Refusing to overwrite existing plots")
  }
  for (index in seq_along(destinations)) {
    if (!file.link(staged_files[[index]], destinations[[index]])) {
      stop("Could not publish every study figure")
    }
    published <- c(published, destinations[[index]])
  }
  publication_complete <- TRUE
  message("Saved verified study figures in ", root)
}

main(commandArgs(trailingOnly = TRUE))
