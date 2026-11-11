# Plot the fixed ALS summary medians with base R graphics only.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2L || length(args) > 3L) {
  stop("Usage: plot_als_sampling_study.R summary.csv output.svg [output.png]")
}

summary <- utils::read.csv(args[[1]], check.names = FALSE)
required <- c(
  "sample_count",
  "noise_sd",
  "lambda",
  "baseline_rmse_overall_median"
)
if (!all(required %in% names(summary))) {
  stop("Summary CSV is missing one or more required figure columns.")
}
if (!all(is.finite(summary$baseline_rmse_overall_median))) {
  stop("Figure metrics must be finite.")
}

draw_figure <- function() {
  noise_levels <- sort(unique(summary$noise_sd))
  densities <- sort(unique(summary$sample_count))
  y_range <- range(c(0, summary$baseline_rmse_overall_median))
  padding <- max(diff(y_range) * 0.08, abs(y_range[[2]]) * 0.02, 1e-9)
  y_range <- c(max(0, y_range[[1]] - padding), y_range[[2]] + padding)
  colors <- c("#0072B2", "#D55E00", "#009E73")
  symbols <- c(16, 17, 15)
  old_par <- graphics::par(
    mfrow = c(1, length(noise_levels)),
    mar = c(4.2, 4.1, 2.8, 0.6),
    oma = c(0, 0, 1.2, 0)
  )
  on.exit(graphics::par(old_par), add = TRUE)

  for (noise_sd in noise_levels) {
    panel <- summary[summary$noise_sd == noise_sd, , drop = FALSE]
    graphics::plot(
      NA,
      xlim = range(panel$lambda),
      ylim = y_range,
      log = "x",
      xlab = expression(ALS ~ lambda ~ "(log scale)"),
      ylab = "Median baseline RMSE",
      main = paste("Noise SD =", format(noise_sd, trim = TRUE)),
      cex.main = 0.95,
      axes = FALSE
    )
    graphics::axis(1, at = sort(unique(panel$lambda)), labels = TRUE)
    graphics::axis(2)
    graphics::box()
    for (index in seq_along(densities)) {
      rows <- panel[panel$sample_count == densities[[index]], , drop = FALSE]
      rows <- rows[order(rows$lambda), , drop = FALSE]
      graphics::lines(
        rows$lambda,
        rows$baseline_rmse_overall_median,
        col = colors[[index]],
        lwd = 1.5,
        type = "b",
        pch = symbols[[index]]
      )
    }
    graphics::legend(
      "topleft",
      legend = paste(densities, "samples"),
      col = colors[seq_along(densities)],
      pch = symbols[seq_along(densities)],
      lty = 1,
      lwd = 1.5,
      bty = "n",
      cex = 0.78
    )
  }
  graphics::mtext(
    "Fixed synthetic grid; medians across five paired seeds; no setting selected",
    outer = TRUE,
    side = 3,
    line = 0.1,
    cex = 0.88
  )
}

grDevices::svg(args[[2]], width = 10, height = 4.2, pointsize = 9)
draw_figure()
grDevices::dev.off()

if (length(args) == 3L) {
  grDevices::png(
    args[[3]],
    width = 1800,
    height = 760,
    res = 160,
    pointsize = 9
  )
  draw_figure()
  grDevices::dev.off()
}
