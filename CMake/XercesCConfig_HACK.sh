#!/bin/bash
CONFIG_FILE=$1
sed -i '/add_library(XercesC::XercesC INTERFACE IMPORTED)/i\
if(NOT TARGET XercesC::XercesC)
' "$CONFIG_FILE"
sed -i '/add_library(XercesC::XercesC INTERFACE IMPORTED)/a\
endif()
' "$CONFIG_FILE"
