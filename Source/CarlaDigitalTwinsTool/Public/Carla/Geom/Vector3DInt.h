// Copyright (c) 2021 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once


#include <cmath>
#include <limits>

namespace carla {
namespace geom {

  class Vector3DInt {
  public:

    // =========================================================================
    // -- Public data members --------------------------------------------------
    // =========================================================================

    int32_t x = 0;

    int32_t y = 0;

    int32_t z = 0;

    // =========================================================================
    // -- Constructors ---------------------------------------------------------
    // =========================================================================

    Vector3DInt() = default;

    Vector3DInt(int32_t ix, int32_t iy, int32_t iz)
      : x(ix),
        y(iy),
        z(iz) {}

    // =========================================================================
    // -- Other methods --------------------------------------------------------
    // =========================================================================

    int64_t SquaredLength() const {
      return x * x + y * y + z * z;
    }

    double Length() const {
       return std::sqrt(SquaredLength());
    }

    // =========================================================================
    // -- Arithmetic operators -------------------------------------------------
    // =========================================================================

    Vector3DInt &operator+=(const Vector3DInt &rhs) {
      x += rhs.x;
      y += rhs.y;
      z += rhs.z;
      return *this;
    }

    friend Vector3DInt operator+(Vector3DInt lhs, const Vector3DInt &rhs) {
      lhs += rhs;
      return lhs;
    }

    Vector3DInt &operator-=(const Vector3DInt &rhs) {
      x -= rhs.x;
      y -= rhs.y;
      z -= rhs.z;
      return *this;
    }

    friend Vector3DInt operator-(Vector3DInt lhs, const Vector3DInt &rhs) {
      lhs -= rhs;
      return lhs;
    }

    Vector3DInt &operator*=(int32_t rhs) {
      x *= rhs;
      y *= rhs;
      z *= rhs;
      return *this;
    }

    friend Vector3DInt operator*(Vector3DInt lhs, int32_t rhs) {
      lhs *= rhs;
      return lhs;
    }

    friend Vector3DInt operator*(int32_t lhs, Vector3DInt rhs) {
      rhs *= lhs;
      return rhs;
    }

    Vector3DInt &operator/=(int32_t rhs) {
      x /= rhs;
      y /= rhs;
      z /= rhs;
      return *this;
    }

    friend Vector3DInt operator/(Vector3DInt lhs, int32_t rhs) {
      lhs /= rhs;
      return lhs;
    }

    friend Vector3DInt operator/(int32_t lhs, Vector3DInt rhs) {
      rhs /= lhs;
      return rhs;
    }

    // =========================================================================
    // -- Comparison operators -------------------------------------------------
    // =========================================================================

    bool operator==(const Vector3DInt &rhs) const {
      return (x == rhs.x) && (y == rhs.y) && (z == rhs.z);
    }

    bool operator!=(const Vector3DInt &rhs) const {
      return !(*this == rhs);
    }

    // =========================================================================
    // -- Conversions to UE4 types ---------------------------------------------
    // =========================================================================

    Vector3DInt(const FIntVector &v) = delete;
    Vector3DInt& operator=(const FIntVector &rhs) = delete;

    /// Return a Vector3DInt converted from centimeters to meters.
    Vector3DInt ToMeters() const {
      return *this / 100;
    }

    /// Return a Vector3DInt converted from meters to centimeters.
    Vector3DInt ToCentimeters() const {
      return *this * 100;
    }

    FIntVector ToFIntVector() const {
      return FIntVector{x, y, z};
    }

  };

} // namespace geom
} // namespace carla
