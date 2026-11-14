// Copyright (c) 2020 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include <vector>
#include <string>
#include <Carla/Geom/Vector3D.h>
#include <Carla/Geom/Vector2D.h>


#include "Actor/ProceduralCustomMesh.h"


namespace carla {
namespace geom {

  /// Material that references the vertex index start and end of
  /// a mesh where it is affecting.
  struct MeshMaterial {

    MeshMaterial(
        const std::string &new_name,
        size_t start = 0u,
        size_t end = 0u)
      : name(new_name),
        index_start(start),
        index_end(end) {}

    const std::string name;

    size_t index_start;

    size_t index_end;

  };

  /// Mesh data container, validator and exporter.
  class Mesh {
  public:

    using vertex_type = Vector3D;
    using normal_type = Vector3D;
    using index_type = size_t;
    using uv_type = Vector2D;
    using material_type = MeshMaterial;

    // =========================================================================
    // -- Constructor ----------------------------------------------------------
    // =========================================================================

    Mesh(const std::vector<vertex_type> &vertices = {},
        const std::vector<normal_type> &normals = {},
        const std::vector<index_type> &indexes = {},
        const std::vector<uv_type> &uvs = {})
      : _vertices(vertices),
        _normals(normals),
        _indexes(indexes),
        _uvs(uvs) {}

    // =========================================================================
    // -- Validate methods -----------------------------------------------------
    // =========================================================================

    /// Check if the mesh can be valid or not.
    bool IsValid() const;

    // =========================================================================
    // -- Mesh build methods ---------------------------------------------------
    // =========================================================================

    /// Adds a triangle strip to the mesh, vertex order is counterclockwise.
    void AddTriangleStrip(const std::vector<vertex_type> &vertices);

    /// Adds a triangle fan to the mesh, vertex order is counterclockwise.
    void AddTriangleFan(const std::vector<vertex_type> &vertices);

    /// Appends a vertex to the vertices list.
    void AddVertex(vertex_type vertex);

    /// Appends a vertex to the vertices list.
    void AddVertices(const std::vector<vertex_type> &vertices);

    /// Appends a normal to the normal list.
    void AddNormal(normal_type normal);

    /// Appends a index to the indexes list.
    void AddIndex(index_type index);

    /// Appends a vertex to the vertices list, they will be read 3 in 3.
    void AddUV(uv_type uv);

    /// Appends uvs.
    void AddUVs(const std::vector<uv_type> & uv);

    /// Starts applying a new material to the new added triangles.
    void AddMaterial(const std::string &material_name);

    /// Stops applying the material to the new added triangles.
    void EndMaterial();

    // =========================================================================
    // -- Export methods -------------------------------------------------------
    // =========================================================================

    /// Returns a string containing the mesh encoded in OBJ.
    /// Units are in meters. It is in Unreal space.
    std::string GenerateOBJ() const;

    /// Returns a string containing the mesh encoded in OBJ.
    /// Units are in meters. This function exports the OBJ file
    /// specifically to be consumed by Recast library.
    /// Changes the build face direction and the coordinate space.
    std::string GenerateOBJForRecast() const;

    /// Returns a string containing the mesh encoded in PLY.
    /// Units are in meters.
    std::string GeneratePLY() const;

    // =========================================================================
    // -- Other methods --------------------------------------------------------
    // =========================================================================

    const std::vector<vertex_type> &GetVertices() const;

    std::vector<vertex_type> &GetVertices();

    size_t GetVerticesNum() const;

    const std::vector<normal_type> &GetNormals() const;

    const std::vector<index_type>& GetIndexes() const;

    std::vector<index_type> &GetIndexes();

    size_t GetIndexesNum() const;

    const std::vector<uv_type> &GetUVs() const;

    const std::vector<material_type> &GetMaterials() const;

    /// Returns the index of the last added vertex (number of vertices).
    size_t GetLastVertexIndex() const;

    /// Merges two meshes into a single mesh
    Mesh& ConcatMesh(const Mesh& rhs, int num_vertices_to_link);

    /// Merges two meshes into a single mesh
    Mesh &operator+=(const Mesh &rhs);

    friend Mesh operator+(const Mesh &lhs, const Mesh &rhs);

    // =========================================================================
    // -- Conversions to UE4 types ---------------------------------------------
    // =========================================================================

    operator FProceduralCustomMesh() const {
      FProceduralCustomMesh Mesh;

      const auto &Verts = GetVertices();
      Mesh.Vertices.Reserve(static_cast<int32>(Verts.size()));
      for (const auto &Vertex : Verts)
      {
        // From meters to centimeters
        Mesh.Vertices.Add(FVector{1e2f * Vertex.x, 1e2f * Vertex.y, 1e2f * Vertex.z});
      }

      const auto &Indexes = GetIndexes();
      const size_t NumTris = Indexes.size() / 3;
      Mesh.Triangles.Reserve(static_cast<int32>(NumTris * 3));
      TArray<FTriIndices> TriIndices;
      TriIndices.Reserve(static_cast<int32>(NumTris));
      for (size_t i = 0u; i + 2u < Indexes.size(); i += 3u)
      {
        const size_t Idx0 = Indexes[i];
        // Since Unreal's coords are left handed, invert the last 2 indices.
        const size_t Idx1 = Indexes[i + 2u];
        const size_t Idx2 = Indexes[i + 1u];
        if (Idx0 == 0u || Idx1 == 0u || Idx2 == 0u ||
            Idx0 > Verts.size() || Idx1 > Verts.size() || Idx2 > Verts.size())
        {
          continue;
        }

        FTriIndices Triangle;
        // "-1" since mesh indexes in Unreal starts from index 0.
        Triangle.v0 = static_cast<int32>(Idx0 - 1u);
        Triangle.v1 = static_cast<int32>(Idx1 - 1u);
        Triangle.v2 = static_cast<int32>(Idx2 - 1u);
        Mesh.Triangles.Add(Triangle.v0);
        Mesh.Triangles.Add(Triangle.v1);
        Mesh.Triangles.Add(Triangle.v2);
        TriIndices.Add(Triangle);
      }

      // Compute the normals
      Mesh.Normals.Init(FVector::UpVector, Mesh.Vertices.Num());

      for (const auto &Triangle : TriIndices) {
        FVector Normal;
        const FVector U = Mesh.Vertices[Triangle.v1] - Mesh.Vertices[Triangle.v0];
        const FVector V = Mesh.Vertices[Triangle.v2] - Mesh.Vertices[Triangle.v0];
        Normal.X = (U.Y * V.Z) - (U.Z * V.Y);
        Normal.Y = (U.Z * V.X) - (U.X * V.Z);
        Normal.Z = (U.X * V.Y) - (U.Y * V.X);
        Normal = -Normal;
        Normal = Normal.GetSafeNormal(.0001f);
        if (Normal != FVector::ZeroVector)
        {
          // fix to prevent ugly x-fighting in geometries with very large curvatures,
          // ensures that all road geometry is facing upwards
          if (FVector::DotProduct(Normal, FVector(0,0,1)) < 0)
          {
            Normal = -Normal;
          }
          Mesh.Normals[Triangle.v0] = Normal;
          Mesh.Normals[Triangle.v1] = Normal;
          Mesh.Normals[Triangle.v2] = Normal;
        }
      }

      const auto &UVs = GetUVs();
      Mesh.UV0.Reserve(static_cast<int32>(UVs.size()));
      for (const auto &uv : UVs)
      {
        Mesh.UV0.Add(FVector2D{uv.x, uv.y});
      }

      return Mesh;
    }

  private:

    // =========================================================================
    // -- Private data members -------------------------------------------------
    // =========================================================================

    std::vector<vertex_type> _vertices;

    std::vector<normal_type> _normals;

    std::vector<index_type> _indexes;

    std::vector<uv_type> _uvs;

    std::vector<material_type> _materials;
  };

} // namespace geom
} // namespace carla
