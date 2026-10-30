# Plot verified paired synthetic campaign evidence without rerunning the optimizer.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop("Usage: Rscript scripts/plot_synthetic_campaigns.R /path/to/comparison")
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
required <- c("report.json", "evaluations.csv", "success-trajectories.csv")
if (
  !identical(seal$schema_version, 1L) || !all(required %in% names(seal$files))
) {
  stop("The comparison completion manifest is incomplete")
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
    stop("Comparison evidence failed integrity verification: ", name)
  }
}
report <- jsonlite::fromJSON(
  file.path(root, "report.json"),
  simplifyVector = FALSE
)
if (!identical(report$profile, "synthetic-isocratic-paired-comparison/1")) {
  stop("Unsupported comparison profile")
}
curves <- utils::read.csv(file.path(root, "success-trajectories.csv"))
evaluations <- utils::read.csv(file.path(root, "evaluations.csv"))
policies <- c("random", "bo_mcp_gp_logei")
series <- c("strict_analytical_accepted", "finite_resolution_score_ge_2")
keys <- paste(curves$policy, curves$series, curves$high_fidelity_budget)
if (
  nrow(curves) != 40L ||
    anyDuplicated(keys) ||
    !setequal(curves$policy, policies) ||
    !setequal(curves$series, series) ||
    any(curves$cohort_size != 6L) ||
    any(
      curves$success_count +
        curves$unknown_success_count +
        curves$observed_no_success_count !=
        6L
    ) ||
    any(curves$success_fraction != curves$success_count / 6) ||
    any(
      curves$success_fraction_upper_bound !=
        (curves$success_count + curves$unknown_success_count) / 6
    )
) {
  stop("Trajectory rows do not preserve the complete declared cohort")
}
expected_keys <- expand.grid(policy = policies, series = series, budget = 1:10)
if (!setequal(keys, with(expected_keys, paste(policy, series, budget)))) {
  stop("Trajectory rows do not cover every declared policy, series and budget")
}
curves$policy <- factor(curves$policy, levels = policies)
curves$series <- factor(
  curves$series,
  levels = series,
  labels = c(
    "Strict analytical acceptance",
    "Finite-resolution utility at least 2"
  )
)
palette <- c(random = "#245D8F", bo_mcp_gp_logei = "#AD6800")
policy_labels <- c(random = "Random", bo_mcp_gp_logei = "BO-MCP GP / qLogEI")
styles <- c(random = "dashed", bo_mcp_gp_logei = "solid")
chart_theme <- ggplot2::theme_minimal(base_size = 11, base_family = "sans") +
  ggplot2::theme(
    legend.position = "top",
    legend.title = ggplot2::element_blank(),
    panel.grid.minor = ggplot2::element_blank(),
    axis.line = ggplot2::element_line(colour = "#404040", linewidth = 0.25),
    plot.title = ggplot2::element_text(face = "bold", size = 15),
    plot.subtitle = ggplot2::element_text(size = 10),
    plot.caption = ggplot2::element_text(hjust = 0, size = 9),
    strip.text = ggplot2::element_text(face = "bold"),
    plot.margin = ggplot2::margin(12, 18, 12, 12)
  )
success_plot <- ggplot2::ggplot(
  curves,
  ggplot2::aes(
    x = high_fidelity_budget,
    y = success_fraction,
    colour = policy,
    linetype = policy,
    group = policy
  )
) +
  ggplot2::geom_vline(
    xintercept = 3.5,
    colour = "#777777",
    linetype = "dotted"
  ) +
  ggplot2::geom_linerange(
    ggplot2::aes(ymin = success_fraction, ymax = success_fraction_upper_bound),
    linewidth = 2,
    alpha = 0.25
  ) +
  ggplot2::geom_step(linewidth = 0.8) +
  ggplot2::geom_point(size = 1.6, ggplot2::aes(shape = policy)) +
  ggplot2::facet_wrap(~series, nrow = 1) +
  ggplot2::scale_colour_manual(values = palette, labels = policy_labels) +
  ggplot2::scale_linetype_manual(values = styles, labels = policy_labels) +
  ggplot2::scale_shape_manual(
    values = c(random = 1, bo_mcp_gp_logei = 16),
    labels = policy_labels
  ) +
  ggplot2::scale_x_continuous(breaks = 1:10, limits = c(1, 10)) +
  ggplot2::scale_y_continuous(
    breaks = seq(0, 1, 0.25),
    limits = c(0, 1),
    labels = function(x) paste0(x * 100, "%")
  ) +
  ggplot2::labs(
    title = "Cumulative success in predeclared synthetic campaigns",
    subtitle = "Six campaigns per policy: two constructed tasks and three paired seeds",
    x = "Synthetic evaluations, including three initial runs",
    y = "Fraction of all planned campaigns",
    caption = paste(
      "Faint vertical ranges include unresolved outcomes; they are not confidence intervals.",
      "The dotted line marks the end of common initialization. No physical instrument was run.",
      sep = "\n"
    )
  ) +
  chart_theme

observed <- evaluations[
  evaluations$status == "consumed" & is.finite(evaluations$score),
]
if (nrow(observed)) {
  observed <- observed[
    order(observed$task_id, observed$seed, observed$policy, observed$ordinal),
  ]
  observed$group <- interaction(
    observed$task_id,
    observed$seed,
    observed$policy,
    drop = TRUE
  )
  observed$best_recorded_utility <- ave(
    observed$score,
    observed$group,
    FUN = cummax
  )
  observed$evaluation_index <- observed$ordinal + 1L
  observed$policy <- factor(observed$policy, levels = policies)
  utility_plot <- ggplot2::ggplot(
    observed,
    ggplot2::aes(
      x = evaluation_index,
      y = best_recorded_utility,
      colour = policy,
      linetype = policy,
      group = policy
    )
  ) +
    ggplot2::geom_hline(
      yintercept = 2,
      colour = "#777777",
      linetype = "dotted"
    ) +
    ggplot2::geom_vline(
      xintercept = 3.5,
      colour = "#777777",
      linetype = "dotted"
    ) +
    ggplot2::geom_step(linewidth = 0.8) +
    ggplot2::facet_grid(task_id ~ seed, labeller = ggplot2::label_both) +
    ggplot2::scale_colour_manual(values = palette, labels = policy_labels) +
    ggplot2::scale_linetype_manual(values = styles, labels = policy_labels) +
    ggplot2::scale_x_continuous(breaks = c(1, 3, 5, 7, 10), limits = c(1, 10)) +
    ggplot2::expand_limits(y = c(0, 2)) +
    ggplot2::labs(
      title = "Best recorded finite-resolution utility for each task and seed",
      subtitle = "Both policies use the same initial coordinates; each arm counts its own evaluations",
      x = "Synthetic evaluations, including initialization",
      y = "Best recorded utility",
      caption = paste(
        "Lines stop at the last consumed evaluation; missing future runs are not filled in.",
        "Utility at least 2 is a separate criterion from strict analytical acceptance.",
        sep = "\n"
      )
    ) +
    chart_theme
  endpoints <- do.call(
    rbind,
    lapply(split(observed, observed$group), function(rows) {
      rows[nrow(rows), , drop = FALSE]
    })
  )
  near_threshold <- endpoints[
    endpoints$policy == "bo_mcp_gp_logei" &
      endpoints$best_recorded_utility < 2 &
      endpoints$best_recorded_utility > 1.99,
  ]
  if (nrow(near_threshold)) {
    near_threshold$endpoint_label <- ifelse(
      2 - near_threshold$best_recorded_utility < 1e-6,
      paste0(
        format(2 - near_threshold$best_recorded_utility, digits = 3),
        " below 2"
      ),
      sprintf("%.6f < 2", near_threshold$best_recorded_utility)
    )
    utility_plot <- utility_plot +
      ggplot2::geom_text(
        data = near_threshold,
        ggplot2::aes(
          x = evaluation_index - 0.15,
          y = best_recorded_utility - 0.1,
          label = endpoint_label
        ),
        hjust = 1,
        vjust = 1,
        size = 3,
        show.legend = FALSE
      )
  }
}
plots <- list(success = success_plot)
if (nrow(observed)) {
  plots$utility <- utility_plot
}
destinations <- unlist(lapply(names(plots), function(name) {
  file.path(root, paste0("synthetic-", name, c(".png", ".svg")))
}))
if (any(file.exists(destinations))) {
  stop("Refusing to overwrite existing plots")
}
for (name in names(plots)) {
  for (extension in c("png", "svg")) {
    path <- file.path(root, paste0("synthetic-", name, ".", extension))
    ggplot2::ggsave(
      path,
      plots[[name]],
      width = 11,
      height = if (name == "success") 5 else 7,
      dpi = 170,
      bg = "white",
      device = if (extension == "svg") grDevices::svg else "png"
    )
  }
}
if (digest::digest(file = seal_path, algo = "sha256") != seal_digest) {
  stop("The comparison manifest changed during plotting")
}
message("Saved verified synthetic campaign figures in ", root)
