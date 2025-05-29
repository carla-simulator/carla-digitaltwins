#!/usr/bin/env python
import unreal
import argparse
import json
import os
import subprocess




ue4_root = unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir())
if ue4_root is None:
    raise("UE4_ROOT environment variable is not set.")
else:
    print(f"UE4_ROOT: {ue4_root}")

uproject_path = unreal.Paths.get_project_file_path()
run = "-run=%s" % ("GenerateTileCommandlet")

argparser = argparse.ArgumentParser()

argparser.add_argument(
    '-s', '--paramstring',
    metavar='S',
    default='',
    type=str,
    help='String to put as arguments')
args = argparser.parse_args()

arguments = args.paramstring

if os.name == "nt":
    sys_name = "Win64"
    editor_path = "%sBinaries/%s/UE4Editor" % (ue4_root, sys_name)
    command = [editor_path, uproject_path, run]
    command.extend(arguments)
    full_command = editor_path + " " + uproject_path + " " + run + " " + arguments
    print("Commandlet:", command)
    print("Arguments:", arguments)
    subprocess.run(full_command, shell=True).ycheck_returncode()
elif os.name == "posix":
    sys_name = "Linux"
    editor_path = "%sBinaries/%s/UE4Editor" % (ue4_root, sys_name)
    full_command = editor_path + " " + uproject_path + " " + run + " " + arguments
    print("Commandlet:", full_command)
    print("Arguments:", arguments)
    subprocess.run(full_command, shell = True).check_returncode()

