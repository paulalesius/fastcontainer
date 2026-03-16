#!/bin/bash

# Stay within this flat directory only
CONTAINERS_DIR=/disk/container
CONTAINER_NAME=ubuntu-noble
CONTAINER_NAME_PREPARE=_${CONTAINER_NAME}-$RANDOM
CONTAINER_PREPARE_HASH=$(sha1sum $CONTAINERS_DIR/$CONTAINER_NAME/PREPARE.sh | awk '{print $1}')
CONTAINER_NAME_FINAL=${CONTAINER_NAME}-${CONTAINER_PREPARE_HASH}

sudo btrfs subvol snapshot $CONTAINERS_DIR/$CONTAINER_NAME $CONTAINERS_DIR/$CONTAINER_NAME_PREPARE && \
# Now run the prepare script inside of the prepared subvolume
sudo systemd-nspawn -D $CONTAINERS_DIR/$CONTAINER_NAME_PREPARE \
	--tmpfs=/var/tmp \
	--private-users=no \
	--resolv-conf=replace-stub \
	/bin/bash /PREPARE.sh && \
	echo "Saving $CONTAINERS_DIR/$CONTAINER_NAME_PREPARE -> $CONTAINERS_DIR/$CONTAINER_NAME_FINAL" && \
	sudo btrfs subvol snapshot $CONTAINERS_DIR/$CONTAINER_NAME_PREPARE $CONTAINERS_DIR/$CONTAINER_NAME_FINAL && \
	echo "Deleting preparation volume $CONTAINERS_DIR/$CONTAINER_NAME_PREPARE" && \
	sudo btrfs subvol delete -c $CONTAINERS_DIR/$CONTAINER_NAME_PREPARE
