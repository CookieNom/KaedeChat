#!/usr/bin/env bash

set -Eeuo pipefail

SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
# shellcheck source=../setup-inputs.sh
source "$SOURCE_ROOT/deploy/setup-inputs.sh"

assert_equal() {
  local expected=$1 actual=$2 label=$3
  [[ $actual == "$expected" ]] || {
    printf '%s: expected %s, received %s\n' "$label" "$expected" "$actual" >&2
    exit 1
  }
}

assert_rejected() {
  local parser=$1 value=$2
  if "$parser" "$value" >/dev/null 2>&1; then
    printf '%s unexpectedly accepted %q\n' "$parser" "$value" >&2
    exit 1
  fi
}

assert_equal 250000 "$(kaede_parse_count 250K)" 'count K suffix'
assert_equal 2500000 "$(kaede_parse_count 2.5m)" 'fractional count suffix'
assert_equal 1000000000 "$(kaede_parse_count 1B)" 'count B suffix'
assert_equal 9223372036854775807 "$(kaede_parse_count 9223372036854775807)" 'maximum signed 64-bit setup integer'
assert_equal 1500000000 "$(kaede_parse_bytes 1.5GB)" 'decimal byte suffix'
assert_equal 1610612736 "$(kaede_parse_bytes 1.5GiB)" 'IEC byte suffix'
assert_equal 536870912 "$(kaede_parse_bytes 0.5GiB)" 'sub-unit byte size'
assert_equal 107374182400 "$(kaede_parse_bytes 100G)" 'short binary suffix'
assert_equal 2.5M "$(kaede_format_count 2500000)" 'fractional count formatter'
assert_equal 250K "$(kaede_format_count 250000)" 'count formatter'
assert_equal 2GiB "$(kaede_format_bytes 2147483648)" 'byte formatter'
assert_equal 100GB "$(kaede_format_bytes 100000000000)" 'decimal byte formatter'
assert_equal 1.5GB "$(kaede_format_bytes 1500000000)" 'fractional decimal byte formatter'

assert_rejected kaede_parse_count 1.2
assert_rejected kaede_parse_count 5T
assert_rejected kaede_parse_bytes 12XB
assert_rejected kaede_parse_bytes 0
assert_rejected kaede_parse_bytes 999999999999999999.999TiB
assert_rejected kaede_parse_count 999999999999999999.999B
assert_rejected kaede_parse_count 9223372036854775808

printf 'setup input parser tests passed\n'
