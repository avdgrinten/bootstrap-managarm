#----------------------------------------------------------------------------------------
# "base" stage that contains all packages that we need.
#----------------------------------------------------------------------------------------
FROM debian:bullseye AS base

RUN apt-get update \
	&& apt-get -y install \
		# Don't install recommended stuff
		--no-install-recommends \
		# Used by gnutls
		autogen \
		# Used by host-bison
		autopoint \
		# Used by binutils in the build process
		bison \
		# Contains gcc and g++
		build-essential \
		# Used by several packages
		cmake \
		# Used during image generation
		cpio \
		# Used by protobuf
		curl \
		# Used by binutils in the build process
		flex \
		# Required by flex but not a hard dependency in Debian.
		libfl2 \
		# Used by many tools in the build process
		gawk \
		# Used by libidn2
		gengetopt \
		# Used by host-gettext
		gettext \
		# Git is used extensively in the build process
		git \
		# Used by eudev
		gperf \
		# Used by libiconv
		groff \
		# Used by libtool
		help2man \
		# Used by shared-mime-info
		itstool \
		# Used by wayland-scanner
		libexpat1-dev \
		# Used by gtk+-2
		libgdk-pixbuf2.0-bin \
		# Used by gdk-pixbuf
		libglib2.0-dev-bin \
		# GMP, MPFR and MPC are used by gcc
		libgmp-dev \
		libmpc-dev \
		libmpfr-dev \
		# Used by cmake
		libssl-dev \
		# Used by ace
		libpng-dev \		
		# Used by many gnu build steps		
		m4 \
		# Required to build images for Android's fastboot protocol.
		mkbootimg \
		# Used by limine
		mtools \
		# Used by libjpeg-turbo for performance reasons
		nasm \
		# Used by groff
		netpbm \
		# Used by Managarm
		ninja-build \
		# To build the certificates, we need trust, provided by p11-kit
		p11-kit \
		# Various build systems invoke pkg-config
		pkg-config \
		# Used by itstool
		python3-libxml2 \
		# Used by mesa
		python3-mako \
		# We need pip, setuptools and wheel for installing python packages
		python3-pip \
		python3-setuptools \
		python3-wheel \
		# Used by managarm
		python3-protobuf \
		# Several programs use rsync
		rsync \
		# makeinfo is used in gnu build systems
		texinfo \
		# Provides 'mkimage', required to build Allwinner D1 images.
		u-boot-tools \
		# Used by protobuf
		unzip \
		# We use wget to fetch some stuff further down
		wget \
		# Used by xcursor-themes
		x11-apps \
		# Used by eudev
		xsltproc \
		# Used by various programs, for example, it is used by file
		zlib1g-dev \
		# Cleanup time
		&& apt-get clean

# Meson is outdated in debian repos, xbstrap and bragi are used by the build process.
RUN pip3 install meson \
	&& pip3 install xbstrap \
	&& pip3 install bragi

# Install mdbook.
RUN cd /root && \
	wget -O mdbook.tar.gz https://github.com/rust-lang/mdBook/releases/download/v0.3.7/mdbook-v0.3.7-x86_64-unknown-linux-gnu.tar.gz && \
	tar xf mdbook.tar.gz && \
	install -D mdbook /usr/local/bin/mdbook

#----------------------------------------------------------------------------------------
# "user" stage that also adds a user account.
#----------------------------------------------------------------------------------------
FROM base AS user
ARG USER=1000

RUN useradd -ms /bin/bash managarm-buildenv -u $USER
USER managarm-buildenv
WORKDIR /home/managarm-buildenv

RUN git config --global user.email "managarm-buildenv@localhost" \
	&& git config --global user.name "managarm-buildenv"

CMD ["bash"]
