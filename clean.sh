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
    if [[ ! -z $(find . -mindepth 1 -maxdepth 1 -name data) ]]; then
        rm -rf data
        if [[ $? == 0 ]]; then
            echo "已删除$(pwd)/data"
        fi
    fi

	if [[ $(count_dir) == 0 ]]; then
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
    printf "%s\n" "输入参数" 
	exit 1
fi