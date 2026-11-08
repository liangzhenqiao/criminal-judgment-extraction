* Purpose: minimal reuse example, sentence length on prior-record indicators with year and province fixed effects.
* Input: china_criminal_judgment_records_1985_2021.csv.gz, decompressed to a temp file and then read, run from this deposit directory.
* Runtime: about 4 minutes on the full file.
clear all
set more off
local csv "`c(tmpdir)'/cjo_v2_11.csv"
! gzip -dc "china_criminal_judgment_records_1985_2021.csv.gz" > "`csv'"
import delimited "`csv'", clear encoding(utf-8) maxquotedrows(10000)

gen province = ustrregexra(id, "^.*?[（(][0-9]{4}[)）]", "")
replace province = usubstr(province, 1, 1)
replace province = "" if province == ""
replace province = "" if !ustrpos("京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川贵云藏陕甘青宁新兵", province)

keep if inrange(judgment_year, 2014, 2021) & !missing(sentence_months)
drop if province == ""

encode province, gen(prov_id)
gen byte prior_flag = .
replace prior_flag = 1 if has_prior_conviction == "True"
replace prior_flag = 0 if has_prior_conviction == "False"
keep sentence_months prior_flag prior_count judgment_year prov_id

regress sentence_months i.prior_flag prior_count i.judgment_year i.prov_id, vce(cluster prov_id)
