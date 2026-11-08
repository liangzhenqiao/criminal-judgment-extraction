# Purpose: minimal reuse example, sentence length on prior-record indicators with year and province fixed effects.
# Input: sample_5000.parquet (same columns as the full record file); swap in the full parquet for the full run.
# Runtime: about 2 seconds on the sample, a few minutes on the full file.
args <- commandArgs(trailingOnly = FALSE)
script <- sub("^--file=", "", args[grep("^--file=", args)])
here <- if (length(script)) dirname(normalizePath(script)) else getwd()

library(arrow)
library(data.table)
library(fixest)

d <- as.data.table(read_parquet(file.path(here, "sample_5000.parquet")))

PROV <- strsplit("京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川贵云藏陕甘青宁新兵", "")[[1]]
d[, province := {
  p <- sub("^.*[（(][0-9]{4}[)）]", "", id)
  p <- substr(p, 1, 1)
  p[!p %in% PROV] <- NA_character_
  p
}]
d[, year := as.integer(judgment_year)]

d <- d[year >= 2014 & year <= 2021 & !is.na(sentence_months)]

m <- feols(sentence_months ~ has_prior_conviction + prior_count | year + province,
           data = d, cluster = ~province)

cat("rows used:", nrow(d), " rows with a province:", sum(!is.na(d$province)), "\n")
etable(m, digits = 3, fitstat = c("n", "r2"))
cat("\nfull summary\n")
summary(m)
