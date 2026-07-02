# Runbook — Restore etcd from a v0.11.x chart snapshot

Applies to HA clusters deployed with `kubernetes-cluster` chart v0.11.0+
(external etcd, not stacked). For v0.10.x (stacked etcd) the recovery
path is "redeploy the cluster" — there is no snapshot mechanism.

## When to use this

- 1 or 2 CPs died but at least one survived with its
  `/var/lib/etcd-backups/etcd-*.snap` intact.
- All 3 CPs died and you copied a snapshot off-host before they went
  down.
- Logical data corruption you need to roll back through.

If all 3 CPs died AND no off-host snapshot exists: full redeploy is
the only path. There's no recovery from this in v0.11.0; remote-copy
of snapshots is queued for v0.11.1.

## Snapshot inventory

The chart's CronJob writes to `/var/lib/etcd-backups/etcd-<TS>.snap`
on every CP, every 6h (default — overridable via
`controlPlane.etcd.snapshotSchedule`). Retention is 7 days
(`snapshotRetentionDays`). Snapshots are not replicated between CPs —
each CP holds its own copies.

To list what's available:

```bash
ssh ironic@<surviving-cp> 'sudo ls -lah /var/lib/etcd-backups'
```

Pick the most recent snapshot newer than your incident.

## Restore procedure — 3-member cluster, restore-in-place

Assumes: blade06/05/04 with OOB IPs 172.19.74.{194,140,185} (same as
the deployment). Substitute your own.

### 1. Stop etcd on every CP

```bash
for cp in blade06 blade05 blade04; do
  ssh ironic@$cp 'sudo systemctl stop etcd'
done
```

### 2. Move the data directory aside (don't delete — you may want it for forensics)

```bash
for cp in blade06 blade05 blade04; do
  ssh ironic@$cp 'sudo mv /var/lib/etcd /var/lib/etcd.broken'
done
```

### 3. On EACH CP, restore from its local snapshot

The restore command takes the snapshot, the new `initial-cluster=`
(same as deploy values), and the LOCAL member identity (name +
advertise peer URL). It produces a fresh `/var/lib/etcd` ready to
start a brand-new raft cluster from the snapshot's data.

```bash
# On blade06
SNAP=/var/lib/etcd-backups/etcd-<TS>.snap
sudo ETCDCTL_API=3 etcdctl snapshot restore "$SNAP" \
  --name blade06 \
  --initial-cluster "blade06=https://172.19.74.194:2380,blade05=https://172.19.74.140:2380,blade04=https://172.19.74.185:2380" \
  --initial-cluster-token etcd-ettore-ha \
  --initial-advertise-peer-urls https://172.19.74.194:2380 \
  --data-dir /var/lib/etcd

# On blade05 (using BLADE05's snapshot file — DO NOT copy blade06's)
sudo ETCDCTL_API=3 etcdctl snapshot restore "$SNAP" \
  --name blade05 \
  --initial-cluster "blade06=...,blade05=...,blade04=..." \
  --initial-cluster-token etcd-ettore-ha \
  --initial-advertise-peer-urls https://172.19.74.140:2380 \
  --data-dir /var/lib/etcd

# On blade04 (using BLADE04's snapshot)
sudo ETCDCTL_API=3 etcdctl snapshot restore "$SNAP" \
  --name blade04 \
  --initial-cluster "blade06=...,blade05=...,blade04=..." \
  --initial-cluster-token etcd-ettore-ha \
  --initial-advertise-peer-urls https://172.19.74.185:2380 \
  --data-dir /var/lib/etcd
```

Notes:
- `--initial-cluster-token` must match what the cluster used originally
  (chart renders `etcd-<clusterName>`).
- All 3 CPs must restore from snapshots that are LOGICALLY EQUIVALENT
  (same raft index — easiest if all 3 come from the same hour's
  CronJob run; the CronJob runs on whichever CP wins the
  control-plane-scheduler lottery so usually only one CP has a given
  hour's file, in which case you copy it to the others BEFORE running
  the restore commands).
- Wrong files = split-brain. If unsure, restore on one CP first, copy
  its `/var/lib/etcd` to the others' tarball-style, ALSO replace
  `member/snap/db` with the per-node-restored version — actually
  easier to just `scp` the snapshot file between CPs and re-run
  `etcdctl snapshot restore` on each.

### 4. Start etcd on every CP

```bash
for cp in blade06 blade05 blade04; do
  ssh ironic@$cp 'sudo systemctl start etcd'
done
```

### 5. Verify

```bash
ssh ironic@blade06 'sudo ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/etcd/pki/ca.crt \
  --cert=/etc/etcd/pki/server.crt \
  --key=/etc/etcd/pki/server.key \
  member list && echo --- && \
  sudo ETCDCTL_API=3 etcdctl ... endpoint health'
```

Expect: 3 started members, all 3 healthy.

### 6. Restart kube-apiserver on each CP

apiserver caches etcd connections; force it to reconnect so it picks
up the rewound state:

```bash
for cp in blade06 blade05 blade04; do
  ssh ironic@$cp 'sudo crictl ps --name kube-apiserver -q | xargs -r sudo crictl stop'
done
# kubelet auto-restarts the static pods within ~10s
```

### 7. Validate workload

Check apiserver responds and resource counts match the snapshot
timestamp (within drift tolerance):

```bash
ssh ironic@blade06 'sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl get nodes,pods -A | wc -l'
```

Any workload changes between the snapshot timestamp and now are LOST.

## Restore procedure — single-CP-survived path

When only 1 of 3 CPs has a usable snapshot, the simplest path is:

1. Restore that single CP as a 1-member cluster
   (`--initial-cluster blade06=https://172.19.74.194:2380` only).
2. Bring up the 2 dead CPs as net-new etcd members by re-PXE-deploying
   them via the chart (they'll fresh-bootstrap their etcd from the
   existing 1-member cluster — except wait, that's not the chart's
   flow, the chart does static bootstrap). Easier: tear down + redeploy
   the full chart with `clusterName: <new-name>` and restore the
   snapshot into the fresh deployment.

OR: hand-edit each replica CP's `/etc/etcd/etcd.conf` to set
`initial-cluster-state: existing` and `etcdctl member add` from the
survivor. This is finicky; only do it if downtime tolerance is low.

## Future improvements (queued for v0.11.1)

- Automated remote-copy of snapshots to mgmt cluster or S3 (kills
  the all-3-CPs-die-no-recovery case).
- Restore CronJob hook that handles the cross-CP `scp` for you when
  the schedule fires (so all 3 CPs always have the same snapshot file
  available).
- Snapshot health check (etcdctl snapshot status + age) surfaced as
  a Prometheus metric.
- Snapshot CA-cert validation (catch a key rotation that broke
  snapshots).
