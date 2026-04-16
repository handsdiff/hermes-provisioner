# TARS (formerly StarAgent) — Completed 2026-04-16

## Overview

Fresh provision — StarAgent was an ActiveClaw agent with no custom processes,
crons, or meaningful workspace data. Rename Hub identity StarAgent → tars,
provision as a new Hermes agent for a new owner.

## Current State

- **Type**: ActiveClaw agent on host filesystem (no Docker container)
- **Location**: `/home/niyant/oc/StarAgent/`
- **Hub identity**: `StarAgent` — rename to `tars`
- **Telegram bot token**: (in provisioner DB after migration)

## Migration Steps

### 1. Provision the VM via API

- Agent name: `TARS` (all caps display name, VM name: `slate-tars` — 4 chars triggers prefix)
- Owner email: `darrynbiervliet@gmail.com`
- Owner telegram: `@algopapi`
- Telegram bot token: from old StarAgent config

### 2. Rename StarAgent → tars on Hub

Same pattern as Vela rename:
- Pull latest Hub, backup data
- Stop Hub
- `sed -i 's/StarAgent/tars/g'` across `data/*.json`
- Delete throwaway `tars` from agents.json (created by provisioner)
- Start Hub, verify

### 3. Restore Hub secret on VM

- Verify secret from Hub agents.json (source of truth, NOT local hub.json)
- Replace `hub-slate-tars` integration with the correct secret
- Update config.yaml: `agent_id: tars`
- Fix ws_url path to use `tars`
- Restart hermes

## Prerequisites

- None — ready to execute
