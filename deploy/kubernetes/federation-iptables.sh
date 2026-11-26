#!/bin/sh
# k3d's earlier DNS hook must read Docker's native firewall rules first.
# The two-instance k3s policy table exceeds iptables-nft's netlink batch limit.
exec /bin/aux/xtables-set-mode.sh -m legacy
