#!/bin/sh --noprofile

exit_on_error() {
	result=$1
	code=$2
	message=$3

	if [ $1 != 0 ]; then
		echo $3
		exit $2
	fi
}

log() {
	if [ $# -gt 2 ]; then
		short=$1
		long=$2
		shift; shift
		echo "==== $long @ $(date) ===="
		eval $*|while read line; do
			echo "== $short: $line"
		done
	else
		echo "=== $1 @ $(date)"
	fi
}

date +%s > t_wrapper_start
log "startup" "wrapper started" "echo -e 'hostname: $(hostname)\nkernel: $(uname -a)'"

log "trace" "tracing google" traceroute -w 1 www.google.com
log "env" "environment at startup" env

# determine locally present stage-out method
LOBSTER_LCG_CP=$(command -v lcg-cp)
LOBSTER_GFAL_COPY=$(command -v gfal-copy)
export LOBSTER_LCG_CP LOBSTER_GFAL_COPY

# determine grid proxy needs
LOBSTER_PROXY_INFO=$(command -v grid-proxy-init)

unset PARROT_HELPER
export PYTHONPATH=python:$PYTHONPATH

if [ -z "$LD_LIBRARY_PATH" ]; then
	export LD_LIBRARY_PATH=lib
else
	export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:lib
fi

if [ "x$PARROT_ENABLED" != "x" ]; then
	log "using parrot"
elif [ ! \( -f "/cvmfs/cms.cern.ch/cmsset_default.sh" \
		-a -n "$LOBSTER_PROXY_INFO" \
		-a \( -n "$LOBSTER_GFAL_COPY" -o -n "$LOBSTER_LCG_CP" \) \
		-a -f /cvmfs/cms.cern.ch/SITECONF/local/JobConfig/site-local-config.xml \) ]; then
	if [ -f /etc/cvmfs/default.local ]; then
		log "conf" "trying to determine proxy with" cat /etc/cvmfs/default.local

		cvmfsproxy=$(cat /etc/cvmfs/default.local|perl -ne '$file  = ""; while (<>) { s/\\\n//; $file .= $_ }; my $proxy = (grep /PROXY/, split("\n", $file))[0]; $proxy =~ s/^.*="?|"$//g; print $proxy;')
		# cvmfsproxy=$(awk -F = '/PROXY/ {print $2}' /etc/cvmfs/default.local|sed 's/"//g')
		log "found CVMFS proxy: $cvmfsproxy"
		export HTTP_PROXY=${HTTP_PROXY:-$cvmfsproxy}
	fi

	if [ -n "$OSG_SQUID_LOCATION" ]; then
		export HTTP_PROXY=${HTTP_PROXY:-$OSG_SQUID_LOCATION}
	elif [ -n "$GLIDEIN_Proxy_URL" ]; then
		export HTTP_PROXY=${HTTP_PROXY:-$GLIDEIN_Proxy_URL}
	fi

	# Last safeguard, if everything else fails.  We need a
	# proxy for parrot!
	export FRONTIER_PROXY=${HTTP_PROXY:-$LOBSTER_FRONTIER_PROXY}
	export HTTP_PROXY=${HTTP_PROXY:-$LOBSTER_CVMFS_PROXY}
	export HTTP_PROXY=$(echo $HTTP_PROXY|perl -ple 's/(?<=:\/\/)([^|:;]+)/@ls=split(\/\s\/,`nslookup $1`);$ls[-1]||$1/eg')

	log "using CVMFS proxy: $HTTP_PROXY"
	log "using Frontier proxy: $FRONTIER_PROXY"

	frontier=$(echo $FRONTIER_PROXY|sed -e 's/[]\/$*.^|[]/\\&/g')
	sed -i -e "s/\$HTTP_PROXY\\>/$frontier/" siteconf/JobConfig/site-local-config.xml

	# These are allowed to be modified via the environment
	# passed to the job (e.g. via condor)
	export PARROT_DEBUG_FLAGS=${PARROT_DEBUG_FLAGS:-}
	export PARROT_PATH=${PARROT_PATH:-./bin}
	export PARROT_CVMFS_REPO=\
'*:try_local_filesystem
*.cern.ch:pubkey=<BUILTIN-cern.ch.pub>,url=http://cvmfs.fnal.gov:8000/opt/*;http://cvmfs.racf.bnl.gov:8000/opt/*
*.opensciencegrid.org:pubkey=<BUILTIN-opensciencegrid.org.pub>,url=http://oasis-replica.opensciencegrid.org:8000/cvmfs/*;http://cvmfs.fnal.gov:8000/cvmfs/*;http://cvmfs.racf.bnl.gov:8000/cvmfs/*'

	export PARROT_ALLOW_SWITCHING_CVMFS_REPOSITORIES=TRUE
	export PARROT_CACHE=${WORKER_TMPDIR:-${TMPDIR:-.}}
	# Make sure that the cvmfs cache is actually shared, this can save
	# up to 1 GB per task in disk usage.
	export PARROT_CVMFS_ALIEN_CACHE=${PARROT_CACHE}/cvmfs
	export PARROT_HELPER=$(readlink -f ${PARROT_PATH%bin*}lib/libparrot_helper.so)

	log "parrot helper: $PARROT_HELPER"
	log "cache" "content of $PARROT_CACHE" ls -lt $PARROT_CACHE

	# Variables needed to set symlinks in CVMFS
	# FIXME add heuristic detection?
	# FIXME setting the CMS local site should actually work!
	# export CMS_LOCAL_SITE=${CMS_LOCAL_SITE:-$PWD/siteconf}
	# log "CMS local site: $CMS_LOCAL_SITE"

	export OASIS_CERTIFICATES=${OASIS_CERTIFICATES:-/cvmfs/oasis.opensciencegrid.org/mis/certificates}
	log "OSG certificate location: $OASIS_CERTIFICATES"

	log "testing parrot usage"
	if [ -n "$(ldd $PARROT_PATH/parrot_run 2>&1 | grep 'not found')" ]; then
		log "ldd" "linkage of parrot" ldd $PARROT_PATH/parrot_run
		exit 169
	else
		log "parrot OK"
	fi

	# FIXME the -M could be removed once local site setting via
	# environment works
	log "starting parrot to access CMSSW..."
	exec $PARROT_PATH/parrot_run -M /cvmfs/cms.cern.ch/SITECONF/local=$PWD/siteconf -t "$PARROT_CACHE/ex_parrot_$(whoami)" bash $0 "$*"
	# exec $PARROT_PATH/parrot_run -t "$PARROT_CACHE/ex_parrot_$(whoami)" bash $0 "$*"
fi

log "sourcing CMS setup"
source /cvmfs/cms.cern.ch/cmsset_default.sh || exit_on_error $? 175 "Failed to source CMS"

if [ -z "$LOBSTER_PROXY_INFO" -o \( -z "$LOBSTER_LCG_CP" -a -z "$LOBSTER_GFAL_COPY" \) ]; then
	log "sourcing OSG setup"
	slc=$(egrep "Red Hat Enterprise|Scientific|CentOS" /etc/redhat-release | sed 's/.*[rR]elease \([0-9]*\).*/\1/')
	source /cvmfs/oasis.opensciencegrid.org/osg-software/osg-wn-client/"$LOBSTER_OSG_VERSION"/current/el$slc-$(uname -m)/setup.sh || exit_on_error $? 175 "Failed to source OSG"

	[ -z "$LOBSTER_LCG_CP" ] && export LOBSTER_LCG_CP=$(command -v lcg-cp)
	[ -z "$LOBSTER_GFAL_COPY" ] && export LOBSTER_GFAL_COPY=$(command -v gfal-copy)
fi

log "env" "environment after sourcing startup scripts" env
log "proxy" "proxy information" env X509_USER_PROXY=proxy voms-proxy-info
log "dir" "working directory at startup" ls -l

tar xjf sandbox.tar.bz2 || exit_on_error $? 170 "Failed to unpack sandbox!"

basedir=$PWD

rel=$(echo CMSSW_*)
arch=$(ls $rel/.SCRAM/|grep slc) || exit_on_error $? 171 "Failed to determine SL release!"
old_release_top=$(awk -F= '/RELEASETOP/ {print $2}' $rel/.SCRAM/slc*/Environment) || exit_on_error $? 172 "Failed to determine old releasetop!"

export SCRAM_ARCH=$arch

log "creating new release $rel"
mkdir lobster_release_tmp || exit_on_error $? 173 "Failed to create temporary directory"
cd lobster_release_tmp
scramv1 project -f CMSSW $rel || exit_on_error $? 173 "Failed to create new release"
new_release_top=$(awk -F= '/RELEASETOP/ {print $2}' $rel/.SCRAM/slc*/Environment)
cd $rel
log "preparing sandbox release $rel"
for i in bin cfipython config lib module python src; do
	rm -rf "$i"
	mv "$basedir/$rel/$i" .
	# ls -lR $i
done


log "fixing python paths"
for f in $(find -iname __init__.py); do
	sed -i -e "s@$old_release_top@$new_release_top@" "$f"
done

eval $(scramv1 runtime -sh) || exit_on_error $? 174 "The command 'cmsenv' failed!"
cd "$basedir"

log "top" "machine load" top -Mb\|head -n 50
log "env" "environment before execution" env
log "wrapper ready"
date +%s > t_wrapper_ready

log "dir" "working directory before execution" ls -l

$*
res=$?

log "dir" "working directory after execution" ls -l

log "wrapper done"
log "final return status = $res"

exit $res
