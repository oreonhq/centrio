# Centrio Installer

A modernized GTK installer for Oreon and other Linux distributions.

## Running locally (carefully)
NOTE: do NOT proceed through Centrio on an already installed system due to risks of your system being destroyed, so running it on a VM that uses a live ISO is the most recommended testing method.
`git clone https://github.com/oreonhq/centrio`
`cd centrio && pip3 install -r requirements.txt`
`cd src && python3 -m main`

make sure you have `python3-pip` installed!

## Using for your own distro
If you want to use Centrio in your own distro, make sure to fork this repo and customize the code to your liking. The codebase is a bit messy and a lot of things are hardcoded, but we plan to make everything easier with some sort of config file later on, but for now this is what we have. Feel free to contribute.
