#!/bin/bash
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
		echo "$1"
		uv run main.py --input "$1" --fast
		return
	fi

	local dir
	while IFS= read -r -d '' dir; do
		recur "$dir" || return
	done < <(find . -mindepth 1 -maxdepth 1 -type d -print0)
)

recur "$1"
