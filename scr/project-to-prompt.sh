reset
git ls-files; echo; git ls-files | grep -v "project-to-prompt.sh" | while read line; do echo "### START FILE: $line"; cat $line; echo -e "### END FILE $line\n"; done
