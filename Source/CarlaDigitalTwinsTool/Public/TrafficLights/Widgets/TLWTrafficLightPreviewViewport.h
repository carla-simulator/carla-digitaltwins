// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "Components/StaticMeshComponent.h"
#include "CoreMinimal.h"
#include "EditorViewportClient.h"
#include "Engine/DataTable.h"
#include "PreviewScene.h"
#include "Slate/SceneViewport.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLPole.h"
#include "Widgets/DeclarativeSyntaxSupport.h"
#include "Widgets/SCompoundWidget.h"
#include "Widgets/SViewport.h"

class STrafficLightPreviewViewport : public SCompoundWidget
{
public:
	SLATE_BEGIN_ARGS(STrafficLightPreviewViewport)
	{
	}
	SLATE_END_ARGS()

	/** Construct the preview viewport widget */
	void Construct(const FArguments& InArgs);

	/** Destructor: clear viewport reference to avoid crash */
	virtual ~STrafficLightPreviewViewport();

public:
	UPROPERTY(EditAnywhere, Category = "Light Type Data Table")
	UDataTable* LightTypesTable{nullptr};

	UPROPERTY(EditAnywhere, Category = "Modules Data Table")
	UDataTable* ModulesTable{nullptr};

	UPROPERTY(EditAnywhere, Category = "Poles Data Table")
	UDataTable* PolesTable{nullptr};

	/** Modules */
public:
	UStaticMeshComponent* AddModuleMesh(const FTLPole& Pole, const FTLHead& Head, FTLModule& ModuleData);
	UStaticMeshComponent* AddPoleBaseMesh(const FTLPole& Pole);
	UStaticMeshComponent* AddPoleExtensibleMesh(const FTLPole& Pole);
	UStaticMeshComponent* AddPoleCapMesh(const FTLPole& Pole);
	void AddBackplate(const FTLPole& Pole, const FTLHead& Head);
	void ClearModuleMeshes();
	void ClearPoleMeshes();
	void ClearBackplates();
	void Rebuild(TArray<FTLPole>& Poles);
	void ResetFrame(const UStaticMeshComponent* Comp);

private:
	FVector2D GetAtlasCoordsForLightType(ETLLightType LightType) const;

private:
	TUniquePtr<FPreviewScene> PreviewScene;
	TSharedPtr<FEditorViewportClient> ViewportClient;
	TSharedPtr<FSceneViewport> SceneViewport;
	TSharedPtr<SViewport> ViewportWidget;

	TArray<UStaticMeshComponent*> PoleBaseMeshComponents;
	TArray<UStaticMeshComponent*> PoleExtensibleMeshComponents;
	TArray<UStaticMeshComponent*> PoleCapMeshComponents;
	TArray<UStaticMeshComponent*> ModuleMeshComponents;
	TArray<UStaticMeshComponent*> BackplateComponents;
};
