#!/bin/bash
#
# Checks the Named Configuration then
# Checks all zones in the named.conf
#
# Written by Troy Germain

# Base location of named configuration file
NAMEDCONF="/etc/bind/named.conf"

# Base Path to the Zone Files
ZONEBASE="/etc/bind/deflect"

# Command Path for named- commands
COMPATH="/usr/bin/"

#CHROOT location if applicable, if not just use null definition
CHROOT=""

eval ZONES=( $(sed -e 's/^[ \t]*//' ${CHROOT}${NAMEDCONF} | grep ^zone | grep -v '^//' | awk -F\" '{printf "%s ", $(NF-1)}') )
eval FILES=( $(sed -e 's/^[ \t]*//' ${CHROOT}${NAMEDCONF} | grep ^file | grep -v '^//' | awk -F\" '{printf "%s ", $(NF-1)}') )

${COMPATH}named-checkconf
if [[ $? != 0 ]]; then
        echo "named.conf Configuration Check Failed!"
        exit 1
fi

echo "Named Config Test Passed"

# Loop starts at 1 instead of 0 because of definition for named.ca
for (( LOOP=1; LOOP<${#ZONES[*]}; LOOP=LOOP+1 )); do
        ${COMPATH}named-checkzone ${ZONES[${LOOP}]}  ${CHROOT}${ZONEBASE}${FILES[${LOOP}]}
        if [[ $? != 0 ]]; then
                echo "Check Failed! - ${ZONES[${LOOP}]} against ${CHROOT}${ZONEBASE}${FILES[${LOOP}]}"
                exit 1
        fi
done

echo "All Zone Files pass"
echo "All OK - Safe to Reload!!"
