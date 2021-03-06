#!/bin/bash

# ``shutdown-sahara``

set -o errexit

source $GRENADE_DIR/grenaderc
source $GRENADE_DIR/functions

# We need base DevStack functions for this
source $BASE_DEVSTACK_DIR/functions
source $BASE_DEVSTACK_DIR/stackrc # needed for status directory

source $BASE_DEVSTACK_DIR/lib/tls
source $(dirname $(dirname $BASH_SOURCE))/plugin.sh

set -o xtrace

stop_sahara

# sanity check that service is actually down
ensure_services_stopped sahara-all
