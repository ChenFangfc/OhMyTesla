# Injected with -DCMAKE_PROJECT_INCLUDE (no patch to the Mosquitto sources).
# Mosquitto 2.0.22's CMake links OpenSSL::SSL unconditionally — even with
# WITH_TLS=OFF (lib/CMakeLists.txt:63, src/CMakeLists.txt:166) — so a TLS-less
# CMake build fails at generate time. Provide an empty stand-in target.
if(NOT WITH_TLS AND NOT TARGET OpenSSL::SSL)
  add_library(OpenSSL::SSL INTERFACE IMPORTED GLOBAL)
endif()
