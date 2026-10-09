Name:           wayvoice
Version:        %{wayvoice_version}
Release:        1%{?dist}
Summary:        Local voice input for Linux and Wayland
License:        AGPL-3.0-or-later
URL:            https://github.com/pavlenkosa/wayvoice
Source0:        wayvoice-%{source_version}.tar.gz
BuildRequires:  gcc
BuildRequires:  python3
BuildRequires:  systemd-rpm-macros
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       python3-pip
Requires:       gtk4
Requires:       libadwaita
Requires:       pipewire-utils
Requires:       wl-clipboard
Requires:       libnotify
Requires(post): kmod
Requires(post): systemd-udev
Requires(postun): systemd-udev
%{?systemd_ordering}

# Sources are used directly through prefix-relative launchers; keep their layout.
%global debug_package %{nil}
%global __brp_python_bytecompile %{nil}

%description
GTK4/libadwaita voice input with local recognition, explicit model downloads,
PipeWire recording and clipboard fallback when automatic paste is unavailable.

%prep
%setup -q -n wayvoice-%{source_version}

%build
./scripts/build-ydotool.sh build/ydotool

%install
mkdir -p %{buildroot}%{_bindir} %{buildroot}/usr/lib/wayvoice/app
cp -a app/. %{buildroot}/usr/lib/wayvoice/app/
find %{buildroot}/usr/lib/wayvoice/app -type d -name __pycache__ -prune -exec rm -rf {} +
for script in wayvoice wayvoice-daemon wayvoice-settings wayvoice-engine-setup wayvoice-ydotoold setup-user; do
    install -m 0755 scripts/$script %{buildroot}%{_bindir}/$script
done
mkdir -p %{buildroot}/usr/lib/wayvoice/ydotool
install -m 0755 build/ydotool/* %{buildroot}/usr/lib/wayvoice/ydotool/
install -m 0644 third_party/ydotool/LICENSE %{buildroot}/usr/lib/wayvoice/ydotool/LICENSE
install -m 0644 third_party/ydotool/README.wayvoice.md %{buildroot}/usr/lib/wayvoice/ydotool/README.md
install -D -m 0644 data/80-wayvoice-uinput.rules %{buildroot}%{_udevrulesdir}/80-wayvoice-uinput.rules
mkdir -p %{buildroot}%{_userunitdir}
install -m 0644 systemd/*.service %{buildroot}%{_userunitdir}/
install -D -m 0644 packaging/io.github.stepan.WayVoice.manage-deps.policy %{buildroot}%{_datadir}/polkit-1/actions/io.github.stepan.WayVoice.manage-deps.policy
install -D -m 0644 data/io.github.stepan.WayVoice.desktop %{buildroot}%{_datadir}/applications/io.github.stepan.WayVoice.desktop
install -D -m 0644 data/io.github.stepan.WayVoice.metainfo.xml %{buildroot}%{_datadir}/metainfo/io.github.stepan.WayVoice.metainfo.xml
for kind in scalable symbolic; do
    mkdir -p %{buildroot}%{_datadir}/icons/hicolor/$kind/apps
    install -m 0644 data/icons/hicolor/$kind/apps/*.svg %{buildroot}%{_datadir}/icons/hicolor/$kind/apps/
done

%post
%systemd_user_post wayvoice.service wayvoice-setup.service wayvoice-ydotool.service
# Apply the new seat ACL rule to an existing device, as on native Debian installs.
udevadm control --reload-rules >/dev/null 2>&1 || :
modprobe uinput >/dev/null 2>&1 || :
udevadm trigger --name-match=uinput >/dev/null 2>&1 || :

%preun
%systemd_user_preun wayvoice.service wayvoice-setup.service wayvoice-ydotool.service

%postun
%systemd_user_postun_with_restart wayvoice.service wayvoice-ydotool.service
udevadm control --reload-rules >/dev/null 2>&1 || :

%files
%license LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/wayvoice*
%{_bindir}/setup-user
/usr/lib/wayvoice/
%{_userunitdir}/wayvoice*.service
%{_udevrulesdir}/80-wayvoice-uinput.rules
%{_datadir}/polkit-1/actions/io.github.stepan.WayVoice.manage-deps.policy
%{_datadir}/applications/io.github.stepan.WayVoice.desktop
%{_datadir}/metainfo/io.github.stepan.WayVoice.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg
%{_datadir}/icons/hicolor/symbolic/apps/io.github.stepan.WayVoice-symbolic.svg
