# simple-workflow image
#
# Deliberately minimal: the only reason this image exists is to bake the step
# script into a stable path, because a `command` step can only run code already
# reachable inside the container. fetch_study_documents.py uses the Python
# standard library only, so there is nothing to apt-get or pip install.
#
# Build context is this file's own directory (docker-image-builder.ts derives it
# with `dirname`), so `COPY scripts/` resolves to simple-workflow/scripts/.
# Moving this Dockerfile into a subfolder would break that COPY.

FROM mediforce-golden-image

COPY scripts/ /opt/simple-workflow/scripts/

WORKDIR /workspace
