#!/usr/bin/env bash

# Human-friendly numeric parsers shared by setup.sh and its host-side tests.
# This file only defines functions; sourcing it never changes host state.

_kaede_scaled_integer() {
  local raw=$1 multiplier=$2 value whole fraction fraction_value=0 scale numerator product
  value=${raw// /}
  value=${value//$'\t'/}
  [[ $value =~ ^([0-9]+)(\.([0-9]{1,3}))?$ ]] || return 1
  whole=${BASH_REMATCH[1]}
  fraction=${BASH_REMATCH[3]-}
  while ((${#whole} > 1)) && [[ $whole == 0* ]]; do
    whole=${whole#0}
  done
  if ((${#whole} > 19)) ||
    { ((${#whole} == 19)) && [[ $whole > 9223372036854775807 ]]; }; then
    return 1
  fi
  whole=$((10#$whole))
  scale=1
  numerator=$whole
  if [[ -n $fraction ]]; then
    case ${#fraction} in
      1) scale=10 ;;
      2) scale=100 ;;
      3) scale=1000 ;;
      *) return 1 ;;
    esac
    fraction_value=$((10#$fraction))
  fi
  ((whole <= (9223372036854775807 - fraction_value) / scale)) || return 1
  numerator=$((whole * scale + fraction_value))
  ((numerator > 0)) || return 1
  ((numerator <= 9223372036854775807 / multiplier)) || return 1
  product=$((numerator * multiplier))
  ((product % scale == 0)) || return 1
  printf '%s' "$((product / scale))"
}

kaede_parse_count() {
  local raw=$1 value suffix multiplier
  value=${raw// /}
  value=${value//$'\t'/}
  value=${value,,}
  if [[ $value =~ ^([0-9]+(\.[0-9]{1,3})?)([kmb]?)$ ]]; then
    value=${BASH_REMATCH[1]}
    suffix=${BASH_REMATCH[3]}
  else
    return 1
  fi
  case $suffix in
    '') multiplier=1 ;;
    k) multiplier=1000 ;;
    m) multiplier=1000000 ;;
    b) multiplier=1000000000 ;;
    *) return 1 ;;
  esac
  _kaede_scaled_integer "$value" "$multiplier"
}

kaede_parse_bytes() {
  local raw=$1 value suffix multiplier
  value=${raw// /}
  value=${value//$'\t'/}
  value=${value,,}
  if [[ $value =~ ^([0-9]+(\.[0-9]{1,3})?)([a-z]*)$ ]]; then
    value=${BASH_REMATCH[1]}
    suffix=${BASH_REMATCH[3]}
  else
    return 1
  fi
  case $suffix in
    ''|b) multiplier=1 ;;
    k|ki|kib) multiplier=1024 ;;
    kb) multiplier=1000 ;;
    m|mi|mib) multiplier=1048576 ;;
    mb) multiplier=1000000 ;;
    g|gi|gib) multiplier=1073741824 ;;
    gb) multiplier=1000000000 ;;
    t|ti|tib) multiplier=1099511627776 ;;
    tb) multiplier=1000000000000 ;;
    *) return 1 ;;
  esac
  _kaede_scaled_integer "$value" "$multiplier"
}

kaede_format_count() {
  local value=$1
  [[ $value =~ ^[0-9]+$ ]] || return 1
  _kaede_format_scaled "$value" 1000000000 B && return
  _kaede_format_scaled "$value" 1000000 M && return
  _kaede_format_scaled "$value" 1000 K && return
  printf '%s' "$value"
}

kaede_format_bytes() {
  local value=$1
  [[ $value =~ ^[0-9]+$ ]] || return 1
  _kaede_format_scaled "$value" 1099511627776 TiB && return
  _kaede_format_scaled "$value" 1000000000000 TB && return
  _kaede_format_scaled "$value" 1073741824 GiB && return
  _kaede_format_scaled "$value" 1000000000 GB && return
  _kaede_format_scaled "$value" 1048576 MiB && return
  _kaede_format_scaled "$value" 1000000 MB && return
  _kaede_format_scaled "$value" 1024 KiB && return
  _kaede_format_scaled "$value" 1000 KB && return
  printf '%sB' "$value"
}

_kaede_format_scaled() {
  local value=$1 unit=$2 suffix=$3 whole remainder factor digits fraction
  ((value >= unit)) || return 1
  whole=$((value / unit))
  remainder=$((value % unit))
  if ((remainder == 0)); then
    printf '%s%s' "$whole" "$suffix"
    return
  fi
  for digits in 1 2 3; do
    case $digits in
      1) factor=10 ;;
      2) factor=100 ;;
      3) factor=1000 ;;
    esac
    if ((remainder * factor % unit == 0)); then
      fraction=$((remainder * factor / unit))
      printf '%s.%0*d%s' "$whole" "$digits" "$fraction" "$suffix"
      return
    fi
  done
  return 1
}
