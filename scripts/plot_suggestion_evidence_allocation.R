#!/usr/bin/env Rscript

# Operational coverage from a sealed suggestion-sequence bundle. This is not a
# success-rate comparison or optimizer ranking.

profile <- "measure-service-suggestion-sequence-qualification/1"
required_files <- c("report.json", "component_calls.jsonl", "slots.csv")

check_grid <- function(data, case, ordinal, n_case, n_slot, ordinals) {
  if (
    nrow(data) != n_case * n_slot ||
      anyNA(data[[case]]) ||
      anyDuplicated(data[c(case, ordinal)]) ||
      length(unique(data[[case]])) != n_case ||
      !all(vapply(
        split(data[[ordinal]], data[[case]]),
        function(x) {
          identical(sort(x), ordinals)
        },
        logical(1)
      ))
  ) {
    stop(
      "Evidence does not retain each planned slot exactly once",
      call. = FALSE
    )
  }
}

sha256 <- function(path) digest::digest(file = path, algo = "sha256")
is_link <- function(path) {
  link <- Sys.readlink(path)
  !is.na(link) && nzchar(link)
}

read_bundle <- function(path) {
  if (!dir.exists(path) || is_link(path)) {
    stop("Bundle must be a real directory", call. = FALSE)
  }
  root <- normalizePath(path, mustWork = TRUE)
  seal_path <- file.path(root, "complete.json")
  if (!file.exists(seal_path) || is_link(seal_path)) {
    stop("Bundle is missing regular complete.json", call. = FALSE)
  }
  seal_sha <- sha256(seal_path)
  seal <- jsonlite::fromJSON(seal_path, simplifyVector = FALSE)
  if (
    !identical(seal$schema_version, 1L) ||
      !all(required_files %in% names(seal$files))
  ) {
    stop("Unsupported or incomplete completion manifest", call. = FALSE)
  }
  verify_files <- function() {
    for (name in names(seal$files)) {
      item <- seal$files[[name]]
      file <- file.path(root, name)
      info <- file.info(file)
      if (
        !identical(name, basename(name)) ||
          name %in% c(".", "..") ||
          !file.exists(file) ||
          is_link(file) ||
          isTRUE(info$isdir) ||
          info$size != item$size_bytes ||
          sha256(file) != item$sha256
      ) {
        stop(
          "Evidence failed completion-manifest verification: ",
          name,
          call. = FALSE
        )
      }
    }
  }
  verify_files()

  report <- jsonlite::fromJSON(
    file.path(root, "report.json"),
    simplifyVector = FALSE
  )
  if (!identical(report$profile, profile)) {
    stop("Unsupported suggestion-sequence report profile", call. = FALSE)
  }
  planned <- report$planned_denominators
  expected <- c(12L, 84L, 16L, 160L)
  observed <- unlist(planned[c(
    "component_cases",
    "component_backend_call_slots",
    "durable_campaigns",
    "durable_suggestion_slots"
  )])
  if (
    !is.numeric(observed) ||
      length(observed) != 4L ||
      anyNA(observed) ||
      !identical(as.numeric(observed), as.numeric(expected))
  ) {
    stop(
      "Report denominators differ from the 12 x 7 and 16 x 10 plans",
      call. = FALSE
    )
  }

  lines <- readLines(file.path(root, "component_calls.jsonl"), warn = FALSE)
  if (length(lines) != 84L || !all(nzchar(lines))) {
    stop("Component evidence must contain 84 nonempty JSON rows", call. = FALSE)
  }
  rows <- lapply(lines, jsonlite::fromJSON, simplifyVector = FALSE)
  component <- data.frame(
    case_id = vapply(rows, `[[`, character(1), "case_id"),
    target = vapply(rows, `[[`, character(1), "optimizer_target"),
    prior = vapply(rows, `[[`, character(1), "surrogate_noise_prior"),
    pattern = vapply(rows, `[[`, character(1), "pattern"),
    ordinal = vapply(rows, `[[`, integer(1), "ordinal"),
    status = vapply(rows, `[[`, character(1), "status"),
    attempted = vapply(rows, `[[`, logical(1), "attempted"),
    stringsAsFactors = FALSE
  )
  check_grid(component, "case_id", "ordinal", 12L, 7L, 3:9)
  if (
    !all(
      component$target %in%
        c("historical_utility", "confirmed_acceptance_reward")
    ) ||
      !all(component$prior %in% c("default", "low_noise")) ||
      !all(component$pattern %in% c("all_zero", "all_one", "mixed")) ||
      nrow(unique(component[c("target", "prior", "pattern")])) != 12L ||
      nrow(unique(component[c("case_id", "target", "prior", "pattern")])) != 12L
  ) {
    stop("Unsupported component case metadata", call. = FALSE)
  }
  component$category <- c(
    returned = "returned",
    failed = "failed",
    partial_failure = "failed",
    interrupted_unknown = "failed",
    not_attempted_unknown = "unknown"
  )[component$status]
  if (anyNA(component$category)) {
    stop("Unsupported component slot status", call. = FALSE)
  }
  component$category[
    component$status == "not_attempted_unknown" & component$attempted
  ] <- "failed"
  component$label <- paste(
    ifelse(
      component$target == "historical_utility",
      "Historical utility",
      "Confirmed acceptance reward"
    ),
    ifelse(component$prior == "default", "Default prior", "Low-noise prior"),
    gsub("_", " ", component$pattern),
    sep = " / "
  )

  slots <- utils::read.csv(
    file.path(root, "slots.csv"),
    stringsAsFactors = FALSE,
    check.names = FALSE,
    na.strings = "",
    strip.white = TRUE
  )
  columns <- c(
    "case_id",
    "task_id",
    "surrogate_noise_prior",
    "duplicate_policy",
    "ordinal",
    "status",
    "is_repeat"
  )
  if (anyDuplicated(names(slots)) || !all(columns %in% names(slots))) {
    stop(
      "Durable slot CSV differs from the frozen 160-row schema",
      call. = FALSE
    )
  }
  slots$ordinal <- as.integer(slots$ordinal)
  if (anyNA(slots$ordinal)) {
    stop("Invalid durable ordinal", call. = FALSE)
  }
  slots$is_repeat <- tolower(trimws(as.character(slots$is_repeat)))
  valid_repeat <- is.na(slots$is_repeat) |
    slots$is_repeat %in% c("", "true", "false", "t", "f")
  if (!all(valid_repeat)) {
    stop("Invalid is_repeat value", call. = FALSE)
  }
  slots$repeat_admitted <- slots$status == "consumed" &
    slots$is_repeat %in% c("true", "t")
  check_grid(slots, "case_id", "ordinal", 16L, 10L, 0:9)
  case_rows <- unique(slots[c(
    "case_id",
    "task_id",
    "surrogate_noise_prior",
    "duplicate_policy"
  )])
  if (
    nrow(case_rows) != 16L ||
      nrow(unique(case_rows[-1L])) != 16L ||
      length(unique(slots$task_id)) != 4L ||
      !setequal(
        unique(slots$surrogate_noise_prior),
        c("default", "low_noise")
      ) ||
      !setequal(unique(slots$duplicate_policy), c("reject", "replicate"))
  ) {
    stop(
      "Unsupported durable task, prior or duplicate-policy metadata",
      call. = FALSE
    )
  }
  slots$category <- c(
    consumed = "consumed",
    duplicate_rejected = "duplicate_rejected",
    failed = "failed",
    partial_failure = "failed",
    policy_violation = "failed",
    numerical_mismatch = "failed",
    interrupted_unknown = "failed",
    proposal_admitted = "failed",
    not_attempted_unknown = "unknown"
  )[as.character(slots$status)]
  if (anyNA(slots$category)) {
    stop("Unsupported durable slot status", call. = FALSE)
  }
  slots$label <- paste(
    slots$task_id,
    slots$surrogate_noise_prior,
    slots$duplicate_policy,
    sep = " / "
  )

  list(
    root = root,
    report = report,
    seal_sha = seal_sha,
    verify_files = verify_files,
    component = component,
    slots = slots
  )
}

make_plot <- function(bundle) {
  component <- bundle$component
  slots <- bundle$slots
  data <- rbind(
    data.frame(
      panel = "Component calls (12 cases x 7 = 84)",
      case_label = component$label,
      ordinal_label = as.character(component$ordinal),
      status = component$category,
      repeat_admitted = FALSE
    ),
    data.frame(
      panel = "Durable evaluations (16 campaigns x 10 = 160)",
      case_label = slots$label,
      ordinal_label = as.character(slots$ordinal),
      status = slots$category,
      repeat_admitted = slots$repeat_admitted
    )
  )
  data$panel <- factor(data$panel, levels = unique(data$panel))
  data$case_label <- factor(
    data$case_label,
    levels = rev(unique(data$case_label))
  )
  data$ordinal_label <- factor(data$ordinal_label, levels = as.character(0:9))
  colors <- c(
    returned = "#148477",
    consumed = "#148477",
    duplicate_rejected = "#D69E00",
    failed = "#C4443B",
    unknown = "#B7C0C8"
  )
  labels <- c(
    returned = "Returned (component)",
    consumed = "Consumed (durable)",
    duplicate_rejected = "Duplicate rejected",
    failed = "Failed / interrupted",
    unknown = "Not attempted / unknown"
  )
  ggplot2::ggplot(
    data,
    ggplot2::aes(ordinal_label, case_label, fill = status)
  ) +
    ggplot2::geom_tile(
      width = 0.92,
      height = 0.92,
      colour = "white",
      linewidth = 0.3
    ) +
    ggplot2::geom_point(
      data = data[data$repeat_admitted, , drop = FALSE],
      mapping = ggplot2::aes(ordinal_label, case_label),
      inherit.aes = FALSE,
      shape = 21,
      size = 2.3,
      stroke = 0.8,
      colour = "#1B1B1B",
      fill = NA
    ) +
    ggplot2::facet_wrap(~panel, ncol = 2, scales = "free", drop = TRUE) +
    ggplot2::scale_fill_manual(
      values = colors,
      breaks = names(labels),
      labels = labels,
      drop = FALSE
    ) +
    ggplot2::scale_x_discrete(drop = TRUE) +
    ggplot2::scale_y_discrete(drop = TRUE) +
    ggplot2::labs(
      title = "Evidence allocation across fixed synthetic sequences",
      subtitle = "Every tile is one planned slot; all unknown slots remain visible.",
      x = "Recorded ordinal",
      y = NULL,
      fill = NULL,
      caption = "Operational coverage only, not success rates or optimizer ranking. Open circles mark only consumed slots with is_repeat = true."
    ) +
    ggplot2::theme_minimal(base_size = 10) +
    ggplot2::theme(
      legend.position = "bottom",
      legend.text = ggplot2::element_text(size = 8),
      panel.grid = ggplot2::element_blank(),
      strip.text = ggplot2::element_text(face = "bold"),
      axis.text.y = ggplot2::element_text(size = 7),
      plot.title = ggplot2::element_text(face = "bold", size = 15),
      plot.caption = ggplot2::element_text(hjust = 0, size = 9)
    )
}

main <- function(args) {
  if (length(args) != 2L) {
    stop(
      "Usage: Rscript plot_suggestion_evidence_allocation.R /sealed/bundle /new/figure-directory",
      call. = FALSE
    )
  }
  if (
    !all(vapply(
      c("ggplot2", "jsonlite", "digest"),
      requireNamespace,
      logical(1),
      quietly = TRUE
    ))
  ) {
    stop("Install ggplot2, jsonlite and digest before plotting", call. = FALSE)
  }
  bundle <- read_bundle(args[[1L]])
  output <- path.expand(args[[2L]])
  if (is_link(output)) {
    stop("Output path must not be a symlink", call. = FALSE)
  }
  parent <- normalizePath(dirname(output), mustWork = TRUE)
  output <- file.path(parent, basename(output))
  root <- bundle$root
  overlaps <- function(a, b) {
    a == b ||
      startsWith(paste0(a, "/"), paste0(b, "/")) ||
      startsWith(paste0(b, "/"), paste0(a, "/"))
  }
  if (overlaps(output, root)) {
    stop(
      "Figure directory must be separate from the sealed bundle",
      call. = FALSE
    )
  }
  if (!dir.exists(output) && !dir.create(output)) {
    stop("Cannot create figure directory", call. = FALSE)
  }
  if (length(list.files(output, all.files = TRUE, no.. = TRUE))) {
    stop(
      "Figure directory must be empty; existing files are preserved",
      call. = FALSE
    )
  }
  plot <- make_plot(bundle)
  ggplot2::ggsave(
    file.path(output, "suggestion-evidence-allocation.png"),
    plot,
    width = 18,
    height = 12,
    dpi = 170,
    bg = "white"
  )
  ggplot2::ggsave(
    file.path(output, "suggestion-evidence-allocation.svg"),
    plot,
    width = 18,
    height = 12,
    device = grDevices::svg,
    bg = "white"
  )
  bundle$verify_files()
  if (sha256(file.path(root, "complete.json")) != bundle$seal_sha) {
    stop("Sealed bundle changed while plotting", call. = FALSE)
  }
  message("Saved verified figures in ", output)
}

if (sys.nframe() == 0L) {
  main(commandArgs(trailingOnly = TRUE))
}
