
f := native-libexpat
$f_up := libexpat

$f_RUN := $B/withprefix $B/prefixes
$f_RUN += host-protoc cross-binutils native-gcc
$f_RUN += --

# We disable xmlwf to avoid building its documentation.
$f_CONFIGURE := $T/ports/$($f_up)/expat/configure --host=x86_64-managarm --prefix=/usr
$f_CONFIGURE += --without-xmlwf

$f_MAKE_ALL := make all
$f_MAKE_INSTALL := make "DESTDIR=$B/system-root" install

$(call milestone_action,configure-$f install-$f)

configure-$f: | $(call upstream_tag,regenerate-$($f_up))
configure-$f:
	rm -rf $B/native/$f && mkdir -p $B/native/$f
	cd $B/native/$f && $($f_RUN) $($f_CONFIGURE)
	touch $(call milestone_tag,$@)

install-$f: | $(call milestone_tag,configure-$f)
	cd $B/native/$f && $($f_RUN) $($f_MAKE_ALL) && $($f_RUN) $($f_MAKE_INSTALL)
	touch $(call milestone_tag,$@)

