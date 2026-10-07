#!/bin/bash
# Push laptop code to the robot.
# Needs the "k1b" SSH shortcut in ~/.ssh/config (see README).
# Only runtime files go to the robot; repo-only files stay on the laptop.
rsync -av --delete \
  --exclude .venv \
  --exclude __pycache__ \
  --exclude .git \
  --exclude .gitignore \
  --exclude voices \
  --exclude README.md \
  --exclude sync.sh \
  ~/k1b/ k1b:/home/booster/Workspace/k1b/