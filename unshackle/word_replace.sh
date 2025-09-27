#!/bin/bash

# dev utility
# Replace 'envied.' with 'unshackle.' in all files located exactly 2 directories deep

find . -mindepth 1 -maxdepth 3 -type f | while read -r file; do
    sed -i 's/envied\./unshackle\./g' "$file"
done
