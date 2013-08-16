#
# rez_install_cmake
#
# Macro for building and installing the cmake file for a project. The installed
# file will be called <unversioned_package_name>.cmake. This generated file will
# then be included by other projects using this one, via the 'rez_find_packages'
# macro. Note that, if using this macro, you should have a line in your package.yaml
# which includes the cmake file in CMAKE_MODULE_PATH, eg:
# - export CMAKE_MODULE_PATH=$CMAKE_MODULE_PATH:!ROOT!/cmake
#
# Arguments:
#
# DEFAULT ARG: AUTO - if enabled, rez will attempt to discover the
#               standard locations for each named argument.  Named
#               arguments will override this automatic discovery.
#
# DESTINATION: 	relative subdirectory to install the cmake file into.
#
# INCLUDE_DIRS: include directories. Any entries that are non-absolute paths are
# 				assumed to be a subdirectory of this package install.
#
# LIBRARY_DIRS: library directories. Any entries that are non-absolute paths are
# 				assumed to be a subdirectory of this package install.
#
# LIBRARIES: 	libraries to link against.
#
# DEFINITIONS: 	extra cflags.
#


if(NOT REZ_BUILD_ENV)
	message(FATAL_ERROR "Include RezBuild, not this cmake module directly.")
endif(NOT REZ_BUILD_ENV)

include(Utils)

#
# res_install_cmake specific utility macros
#

# Append a directory to relative paths found in input_dirs
macro(append_to_relative_dirs append_dir input_dirs output_dirs)
	# Use a temporary variable to allow input_dirs to be the same
	# variable as output_dirs, for in-line replacement
	set(_temp_input_dirs)
	foreach(_dir ${input_dirs})
		string(REGEX MATCH "^/" is_abs ${_dir})
		if(is_abs)
			list(APPEND _temp_input_dirs "${_dir}")
		else()
			list(APPEND _temp_input_dirs "${append_dir}/${_dir}")
		endif()
	endforeach()
	set(${output_dirs} ${_temp_input_dirs})
endmacro()

# Find dynamic libraries in the library_dirs directory.
macro(find_dynamic_libs library_dirs output_libraries)
    set(_exts so dylib bundle)
    foreach(_dir in ${library_dirs})
        foreach(_ext ${_exts})
            file(GLOB _libs ${_dir}/*.${_ext})
            if(_libs)
                list(APPEND ${output_libraries} "${_libs}")
            endif()
        endforeach()
    endforeach()
endmacro()

macro(rez_install_cmake)

	#
	# parse args
	#

	parse_arguments(INSTCM
		"DESTINATION;INCLUDE_DIRS;LIBRARY_DIRS;LIBRARIES;DEFINITIONS"
		""
		${ARGN})

	#
	# Test required arguments
	list(GET INSTCM_DEFAULT_ARGS 0 do_auto)
	list(GET INSTCM_DESTINATION 0 dest_dir)
	if(NOT do_auto AND NOT dest_dir)
		message(FATAL_ERROR "need to specify DESTINATION in call to install_cmake")
	endif()

	# Defer cmake file creation until after install, so that if do_auto is
	# true, it will be able to correctly find the installed libraries.
	#
	# We always defer this macro, because it keeps the code succinct.
	# NOTE: this does somewhat break backward compatibility in that
	# 	    the cmake file is never written to the build directory.
	set(auto_args "${ARGN} PROJECT_NAME ${REZ_BUILD_PROJECT_NAME}")
	install(CODE "
		set(REZ_BUILD_ENV 1)
		list(APPEND CMAKE_MODULE_PATH ${CMAKE_MODULE_PATH})
		include(RezInstallCMake)
		_rez_install_auto_cmake(${auto_args})
	    set(REZ_BUILD_ENV 0)
	")
endmacro(rez_install_cmake)


macro(_rez_install_auto_cmake)

	#
	# parse args
	#

	parse_arguments(INSTCM
		"DESTINATION;INCLUDE_DIRS;LIBRARY_DIRS;LIBRARIES;DEFINITIONS;PROJECT_NAME"
		""
		${ARGN})

	list(GET INSTCM_DEFAULT_ARGS 0 do_auto)
	list(GET INSTCM_DESTINATION 0 dest_dir)
	set(projname ${INSTCM_PROJECT_NAME})
	string(TOUPPER ${projname} UPNAME)
	set(root_dir "\$ENV{REZ_${UPNAME}_ROOT}")

	#
	# Populate auto arguments
	#

	if(do_auto)
		if(NOT dest_dir)
			set(dest_dir cmake)
		endif()
		if(NOT INSTCM_INCLUDE_DIRS)
			set(INSTCM_INCLUDE_DIRS include)
		endif()
		if(NOT INSTCM_LIBRARY_DIRS)
			set(INSTCM_LIBRARY_DIRS lib)
		endif()
	else()
		#
		# point non-absolute paths at the install dir for this package
		#

	endif()

	# Report to the user that the cmake script is being installed, and where
	# In order to do this properly, we must first make the directory where
	# cmake will be installed
	set(abs_dest_dir "${CMAKE_INSTALL_PREFIX}/${dest_dir}")
	execute_process(COMMAND ${CMAKE_COMMAND} -E make_directory "${abs_dest_dir}")

	set(cmake_file ${projname}.cmake)
	set(cmake_path "${abs_dest_dir}/${cmake_file}")
	message(STATUS "Installing: cmake script to ${cmake_path}")

	#
	# process non-absolute paths to point at the install dir for this package
	#

	append_to_relative_dirs(\${root_dir} "${INSTCM_INCLUDE_DIRS}" inc_dirs)
	append_to_relative_dirs(\${root_dir} "${INSTCM_LIBRARY_DIRS}" lib_dirs)
	append_to_relative_dirs(${CMAKE_INSTALL_PREFIX} "${INSTCM_LIBRARY_DIRS}" lib_installed_dirs)

	if(do_auto AND NOT INSTCM_LIBRARIES)
		# Find the dynamic libs
		find_dynamic_libs(${lib_installed_dirs} libraries)

		# Replace absolute paths with the install dir variable.
		string(REPLACE ${CMAKE_INSTALL_PREFIX} "${root_dir}" libraries "${libraries}")
	else()
		set(libraries "${INSTCM_LIBRARIES}")
	endif()

	#
	# generate the cmake file
	#


	# This won't be made with the correct permissions.  Not sure how to fix this.
	# PERMISSIONS ${REZ_FILE_INSTALL_PERMISSIONS}
	file(WRITE ${cmake_path})
	file(APPEND ${cmake_path} "set(${projname}_INCLUDE_DIRS \"${inc_dirs}\")\n")
	file(APPEND ${cmake_path} "set(${projname}_LIBRARY_DIRS \"${lib_dirs}\")\n")
	file(APPEND ${cmake_path} "set(${projname}_LIBRARIES \"${libraries}\")\n")
	file(APPEND ${cmake_path} "set(${projname}_DEFINITIONS \"${INSTCM_DEFINITIONS}\")\n")

	file(APPEND ${cmake_path} "# Set these as environment variables as well\n")
	file(APPEND ${cmake_path} "set(ENV{${projname}_INCLUDE_DIRS} \"\${${projname}_INCLUDE_DIRS}\")\n")
	file(APPEND ${cmake_path} "set(ENV{${projname}_LIBRARY_DIRS} \"\${${projname}_LIBRARY_DIRS}\")\n")
	file(APPEND ${cmake_path} "set(ENV{${projname}_LIBRARIES} \"\${${projname}_LIBRARIES}\")\n")
	file(APPEND ${cmake_path} "set(ENV{${projname}_DEFINITIONS} \"\${${projname}_DEFINITIONS}\")\n")

endmacro(_rez_install_auto_cmake)






#    Copyright 2008-2012 Dr D Studios Pty Limited (ACN 127 184 954) (Dr. D Studios)
#
#    This file is part of Rez.
#
#    Rez is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Rez is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with Rez.  If not, see <http://www.gnu.org/licenses/>.
