#!/usr/bin/env Rscript
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop("Usage: Rscript scripts/plot_gaussian_qualification.R OUTPUT_DIR")
}
root <- normalizePath(args[[1]], mustWork = TRUE)
seal <- jsonlite::fromJSON(
  file.path(root, "complete.json"),
  simplifyVector = FALSE
)
for (name in names(seal$files)) {
  path <- file.path(root, name)
  expected <- seal$files[[name]]
  if (
    !file.exists(path) ||
      unname(file.info(path)$size) != expected$size_bytes ||
      digest::digest(file = path, algo = "sha256") != expected$sha256
  ) {
    stop(paste("Core output does not match completion seal:", name))
  }
}
report <- jsonlite::fromJSON(
  file.path(root, "report.json"),
  simplifyVector = FALSE
)
if (
  !identical(
    report$profile,
    "measure-service-gaussian-centroid-qualification/1"
  )
) {
  stop("Unsupported qualification profile")
}
counts <- report$numerical_comparison
coverage <- data.frame(
  field = c(
    "Numeric values",
    "Nullable fields",
    "Analyte identities",
    "Pair identities",
    "Status fields"
  ),
  matched = c(
    counts$numeric_match_count,
    counts$null_semantics_match_count,
    counts$analyte_id_elements_match,
    counts$pair_id_elements_match,
    counts$status_field_match_count
  ),
  expected = c(
    counts$numeric_comparison_count,
    counts$null_semantics_check_count,
    counts$analyte_id_element_count,
    counts$pair_id_element_count,
    counts$status_field_count
  )
)
if (
  any(!is.finite(coverage$expected) | coverage$expected <= 0) ||
    any(!is.finite(coverage$matched) | coverage$matched < 0) ||
    any(coverage$matched > coverage$expected)
) {
  stop("Coverage denominators and match counts must remain finite and valid")
}
coverage$fraction <- coverage$matched / coverage$expected
coverage$field <- factor(coverage$field, levels = rev(coverage$field))
coverage$label <- paste(
  format(coverage$matched, big.mark = ",", trim = TRUE),
  "/",
  format(coverage$expected, big.mark = ",", trim = TRUE)
)
plot <- ggplot2::ggplot(coverage, ggplot2::aes(y = field)) +
  ggplot2::geom_col(ggplot2::aes(x = 1), fill = "#EEF2F6", width = 0.65) +
  ggplot2::geom_col(
    ggplot2::aes(x = fraction),
    fill = "#B9DCD8",
    width = 0.65
  ) +
  ggplot2::geom_text(
    ggplot2::aes(x = 0.025, label = label),
    hjust = 0,
    colour = "#203047",
    size = 5
  ) +
  ggplot2::scale_x_continuous(
    limits = c(0, 1),
    breaks = c(0, 0.25, 0.5, 0.75, 1),
    labels = c("0%", "25%", "50%", "75%", "100%"),
    expand = ggplot2::expansion(mult = c(0, 0))
  ) +
  ggplot2::labs(
    title = "Independent checks retain every declared field",
    subtitle = paste0(
      counts$passed_case_count,
      "/",
      counts$case_count,
      " numerical cases · ",
      report$range_and_input_failure_checks$passed_check_count,
      "/",
      report$range_and_input_failure_checks$check_count,
      " input/range checks · cohorts from 1 to 32"
    ),
    x = "Fields matching the reviewed reference",
    y = NULL,
    caption = paste0(
      "Relative tolerance 1e-12; absolute tolerance zero. FWHM factor 2.3548 is explicit.\n",
      "Ideal centroids and assumed widths; no physical-width, signal, instrument or optimizer qualification."
    )
  ) +
  ggplot2::theme_minimal(base_size = 14) +
  ggplot2::theme(
    plot.title = ggplot2::element_text(
      face = "bold",
      colour = "#203047",
      size = 22
    ),
    plot.subtitle = ggplot2::element_text(
      colour = "#52647C",
      margin = ggplot2::margin(b = 18)
    ),
    plot.caption = ggplot2::element_text(
      hjust = 0,
      colour = "#52647C",
      margin = ggplot2::margin(t = 18)
    ),
    plot.title.position = "plot",
    plot.caption.position = "plot",
    panel.grid.minor = ggplot2::element_blank(),
    panel.grid.major.y = ggplot2::element_blank(),
    plot.margin = ggplot2::margin(24, 30, 20, 24)
  )
for (extension in c("png", "svg")) {
  ggplot2::ggsave(
    file.path(root, paste0("gaussian-reference-coverage.", extension)),
    plot,
    width = 11,
    height = 6.5,
    dpi = 180,
    bg = "white",
    device = if (extension == "svg") grDevices::svg else "png"
  )
}
