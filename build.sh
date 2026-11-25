#!/bin/bash
START_DIR="$(pwd)"

count_dir() {
	local dir count=0
	while IFS= read -r -d '' dir; do
		((++count))
	done < <(find . -mindepth 1 -maxdepth 1 -type d -print0)
	printf '%d\n' "$count"
}

recur() (
	cd -- "$1" || return 1

	if [[ $(count_dir) == 0 ]]; then
		dstdir="$(pwd)"
		db_path="$dstdir/data/ocr_fast.db"
		echo "$dstdir"
		uv run "$START_DIR/main.py" --input "$dstdir" --fast --db "$db_path"
		uv run "$START_DIR/main.py" -export --db "$db_path"
		return
	fi

	local dir
	while IFS= read -r -d '' dir; do
		recur "$dir" || return
	done < <(find . -mindepth 1 -maxdepth 1 -type d -print0)
)
if [[ "$#" != 0 ]]; then
	recur "$1"
else
	exit 1
fi

