// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include "Widgets/SCompoundWidget.h"
#include "PreviewScene.h"
#include "EditorViewportClient.h"
#include "Slate/SceneViewport.h"
#include "Widgets/SViewport.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLModule.h"
#include "Materials/MaterialInterface.h"
#include "Materials/MaterialInstanceDynamic.h"

class SViewport;

class STrafficLightPreviewViewport : public SCompoundWidget
{
public:
    SLATE_BEGIN_ARGS(STrafficLightPreviewViewport) {}
    SLATE_END_ARGS()

    /** Construct the preview viewport widget */
    void Construct(const FArguments& InArgs);

    /** Destructor: clear viewport reference to avoid crash */
    virtual ~STrafficLightPreviewViewport();

    /** Head */
public:

    /** Set the head style  */
    void SetHeadStyle(int32 Index, ETLHeadStyle Style);

    void AddBackplateMesh   (int32 HeadIndex);
    void RemoveBackplateMesh(int32 HeadIndex);

    /** Modules */
public:
    UStaticMeshComponent* AddModuleMesh(const FTLHead& Head, const FTLModule& ModuleData);
    void RemoveModuleMeshesForHead(int32 HeadIndex);
    void RemoveModuleMeshForHead(int32 HeadIndex, int32 ModuleIndex);


private:
    FLinearColor InitialColorFor(ETLHeadStyle Style) const;


private:
    /** Holds the preview scene (off-screen world) */
    TUniquePtr<FPreviewScene> PreviewScene;

    /** Viewport client driving the scene */
    TSharedPtr<FEditorViewportClient> ViewportClient;

    /** Slate viewport wrapper */
    TSharedPtr<FSceneViewport> SceneViewport;
    TSharedPtr<SViewport> ViewportWidget;

    /** Mesh components spawned for each head */
    TArray<UStaticMeshComponent*> HeadMeshComponents;

    TArray<UStaticMeshComponent*> BackplateMeshComponents;

    /** Mesh components spawned for each head */
    TArray<UStaticMeshComponent*> ModuleMeshComponents;

    UStaticMesh* CubeMesh{ nullptr };
    UMaterialInterface* BaseMaterial{ nullptr };
    TArray<UMaterialInstanceDynamic*> HeadDynMats;
    TArray<UMaterialInstanceDynamic*> ModuleDynMats;
};
