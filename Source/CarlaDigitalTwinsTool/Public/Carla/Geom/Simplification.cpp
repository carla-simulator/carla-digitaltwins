// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#include "Carla/Geom/Simplification.h"
#include "Carla/Simplify/Simplify.h"

namespace carla {
namespace geom {

  void Simplification::Simplificate(const std::unique_ptr<geom::Mesh>& pmesh){
    if (!pmesh || pmesh->GetVertices().empty() || pmesh->GetIndexes().size() < 3) {
      return;
    }

    Simplify::SimplificationObject Simplification;
    const auto& src_vertices = pmesh->GetVertices();
    const auto& indices = pmesh->GetIndexes();
    Simplification.vertices.reserve(src_vertices.size());
    for (const carla::geom::Vector3D& current_vertex : src_vertices) {
      Simplify::Vertex v;
      v.p.x = current_vertex.x;
      v.p.y = current_vertex.y;
      v.p.z = current_vertex.z;
      Simplification.vertices.push_back(v);
    }

    const size_t num_verts = src_vertices.size();
    Simplification.triangles.reserve(indices.size() / 3);
    for (size_t i = 0; i + 2 < indices.size(); i += 3) {
      if (indices[i] == 0 || indices[i + 1] == 0 || indices[i + 2] == 0 ||
          indices[i] > num_verts || indices[i + 1] > num_verts || indices[i + 2] > num_verts) {
        continue;
      }
      Simplify::Triangle t;
      t.material = 0;
      t.v[0] = static_cast<int>(indices[i] - 1);
      t.v[1] = static_cast<int>(indices[i + 1] - 1);
      t.v[2] = static_cast<int>(indices[i + 2] - 1);
      Simplification.triangles.push_back(t);
    }

    if (Simplification.triangles.empty()) {
      return;
    }

    // Reduce to the X% of the polys
    float target_size = static_cast<float>(Simplification.triangles.size());
    Simplification.simplify_mesh(static_cast<int>(target_size * simplification_percentage));

    auto& dst_vertices = pmesh->GetVertices();
    auto& dst_indices = pmesh->GetIndexes();
    dst_vertices.clear();
    dst_indices.clear();
    dst_vertices.reserve(Simplification.vertices.size());
    dst_indices.reserve(Simplification.triangles.size() * 3);

    for (const Simplify::Vertex& current_vertex : Simplification.vertices) {
      carla::geom::Vector3D v;
      v.x = static_cast<float>(current_vertex.p.x);
      v.y = static_cast<float>(current_vertex.p.y);
      v.z = static_cast<float>(current_vertex.p.z);
      pmesh->AddVertex(v);
    }

    for (size_t i = 0; i < Simplification.triangles.size(); ++i) {
      dst_indices.push_back(static_cast<size_t>(Simplification.triangles[i].v[0]) + 1);
      dst_indices.push_back(static_cast<size_t>(Simplification.triangles[i].v[1]) + 1);
      dst_indices.push_back(static_cast<size_t>(Simplification.triangles[i].v[2]) + 1);
    }
  }

} // namespace geom
} // namespace carla
