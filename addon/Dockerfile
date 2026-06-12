# Home Assistant add-on image for ha-airspace.
#
# Thin wrapper over the published service image (DESIGN §Distribution): it adds
# only the option->config renderer and a run shim. BUILD_FROM is supplied per
# architecture by build.yaml and points at the multi-arch service image, so the
# add-on inherits the same glibc runtime + installed venv — no Alpine/musl
# rebuild of pydantic-core on armv7.
ARG BUILD_FROM=ghcr.io/ifnull/ha-airspace:latest
FROM ${BUILD_FROM}

# The base image runs as the non-root service user; the add-on needs root to
# read the Supervisor-written /data/options.json and write the rendered config.
USER root

COPY render_config.py /app/render_config.py
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Replace the service entrypoint with the shim, which renders the config from
# add-on options and then execs the service.
ENTRYPOINT []
CMD ["/run.sh"]
