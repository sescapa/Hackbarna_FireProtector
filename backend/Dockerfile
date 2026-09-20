# --- ELMFIRE (pinned), built once in its own stage ---------------------------
# Only the compiled binaries reach the runtime image below.
FROM python:3.13-slim-bookworm AS elmfire-builder

RUN apt-get update -y && apt-get install --no-install-recommends -y \
        build-essential ca-certificates gfortran git libopenmpi-dev && \
    rm -rf /var/lib/apt/lists/*

# Pinned to a main-branch commit: the last tag (2025.0717) predates barriers, GAUSSIAN
# perturbations, the UMD spotting model and the percent-unit fuel-moisture perturbation
# that the pipeline relies on (docs call this line "2025.1002").
ARG ELMFIRE_VER=cbf924a7bce2023105ab498fc255a8925e410646
# Upstream bug (still on main): C is a pointer into the burned-cell linked list and is
# uninitialised on the first call, so `IF (ASSOCIATED(C)) DEALLOCATE(C)` aborts with
# "free(): invalid pointer" under recent glibc / gfortran. The intent is to hand a null
# node to SURFACE_SPREAD_RATE, which NULLIFY does.
RUN git clone --filter=blob:none https://github.com/lautenberger/elmfire /opt/elmfire && \
    git -C /opt/elmfire checkout --quiet ${ELMFIRE_VER} && \
    sed -i 's/IF (ASSOCIATED(C)) DEALLOCATE(C)/NULLIFY(C)/' /opt/elmfire/build/source/elmfire_level_set.f90 && \
    grep -q 'NULLIFY(C)' /opt/elmfire/build/source/elmfire_level_set.f90 && \
    # Second upstream bug: the barrier raster is read by one rank and only its *header* is
    # broadcast (unlike the other rasters, it is never placed in MPI shared memory), so with
    # more than one rank CHECK_BARRIER_BREACH dereferences a null pointer (SIGSEGV).
    # Allocate on the other ranks and broadcast the data after the header.
    sed -i 's|^   IF (USE_BARRIERS) CALL MPI_BCAST_RASTER_HEADER(BARRIER_WIDTH, PARALLEL_IO_RANK(41), .FALSE.)$|   IF (USE_BARRIERS) THEN\n      CALL MPI_BCAST_RASTER_HEADER(BARRIER_WIDTH, PARALLEL_IO_RANK(41), .FALSE.)\n      IF (.NOT. ASSOCIATED(BARRIER_WIDTH%R4)) ALLOCATE(BARRIER_WIDTH%R4(1:BARRIER_WIDTH%NCOLS,1:BARRIER_WIDTH%NROWS,1:1))\n      CALL MPI_BCAST(BARRIER_WIDTH%R4, BARRIER_WIDTH%NCOLS*BARRIER_WIDTH%NROWS, MPI_REAL, PARALLEL_IO_RANK(41), MPI_COMM_WORLD, IERR)\n   ENDIF|' /opt/elmfire/build/source/elmfire_io.f90 && \
    grep -q 'MPI_BCAST(BARRIER_WIDTH%R4' /opt/elmfire/build/source/elmfire_io.f90 && \
    cd /opt/elmfire/build/linux && ./make_gnu.sh

# --- API ---------------------------------------------------------------------
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ELMFIRE runs under OpenMPI; fire_spread shells out to gdal_translate / ogr2ogr
# when building run inputs and the static tier. rasterio/pyproj wheels bundle
# their own GDAL and PROJ, so no -dev packages.
RUN apt-get update -y && apt-get install --no-install-recommends -y \
        gdal-bin libgfortran5 openmpi-bin && \
    rm -rf /var/lib/apt/lists/*

ARG ELMFIRE_VER=cbf924a7bce2023105ab498fc255a8925e410646
COPY --from=elmfire-builder /opt/elmfire/build/linux/bin /opt/elmfire/build/linux/bin
ENV ELMFIRE_VER=${ELMFIRE_VER} \
    ELMFIRE_BASE_DIR=/opt/elmfire \
    ELMFIRE_INSTALL_DIR=/opt/elmfire/build/linux/bin \
    PATH=/opt/elmfire/build/linux/bin:$PATH

WORKDIR /srv

# Dependencies first, so editing application code does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY fire_spread ./fire_spread
# Static-tier builder and hindcast tooling, run as `python -m scripts.fire_spread.…`.
COPY scripts/__init__.py ./scripts/__init__.py
COPY scripts/fire_spread ./scripts/fire_spread
# Container-side register loader (scripts/setup_db_container.sh): the INSPIRE extractors,
# the slug map they read, and the schema it applies.
COPY scripts/load_register.py ./scripts/load_register.py
COPY extract_buildings.py extract_forests.py ./
COPY data/municipality_slug_map.csv ./data/municipality_slug_map.csv
COPY db/init ./db/init

# Run as a non-root user. data/fire_spread is the bind mount for the static
# tier and run directories; it must be writable by this uid on Linux hosts.
RUN useradd --create-home --uid 10001 api && \
    mkdir -p /srv/data/fire_spread && chown -R api:api /srv
USER api

EXPOSE 8000

# No curl in the slim image, so check over Python instead.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
