Name:           centrio-installer
Version:        1.0
Release:        9%{?dist}
Summary:        Oreon live installer
License:        GPL-2.0-or-later
URL:            https://github.com/centrio/centrio
BuildArch:      noarch
Source0:        centrio-%{version}.tar.xz
Source1:        liveinst.desktop
Requires:       python3-gobject gtk4 libadwaita
Requires:       polkit
Requires:       zenity
BuildRequires:  python3-devel

%description
Centrio is the Oreon installer. It runs in the live session and in the GIS kiosk.

%prep
%autosetup -n centrio-%{version}

%build
# No compile step for pure Python

%install
%{__mkdir_p} %{buildroot}%{_datadir}/centrio
%{__mkdir_p} %{buildroot}%{_datadir}/centrio/ui
%{__mkdir_p} %{buildroot}%{_datadir}/centrio/icons
%{__mkdir_p} %{buildroot}%{_datadir}/centrio/locale
%{__mkdir_p} %{buildroot}%{_datadir}/applications

# Application and UI (from tarball)
install -p -m 0644 %{_builddir}/centrio-%{version}/src/*.py %{buildroot}%{_datadir}/centrio/
install -p -m 0644 %{_builddir}/centrio-%{version}/src/ui/*.py %{buildroot}%{_datadir}/centrio/ui/
install -p -m 0644 %{_builddir}/centrio-%{version}/icons/*.svg %{buildroot}%{_datadir}/centrio/icons/ 2>/dev/null || true
cp -a %{_builddir}/centrio-%{version}/locale/* %{buildroot}%{_datadir}/centrio/locale/ 2>/dev/null || true

# Live env (from SOURCES)
install -p -m 0644 %{_sourcedir}/liveinst.desktop %{buildroot}%{_datadir}/applications/

%files
%{_datadir}/centrio/
%{_datadir}/applications/liveinst.desktop

%post
%systemd_post oreon-live-marker.service

%preun
%systemd_preun oreon-live-marker.service

%changelog
* Fri Feb 20 2026 Brandon Lester <blester@oreonhq.com> - 1.0-1
- initial package
