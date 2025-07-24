#include "TrafficLights/Widgets/TLWTrafficLightPreviewViewport.h"

#include "Components/StaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "Logging/LogMacros.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Math/MathFwd.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLLightTypeDataTable.h"
#include "TrafficLights/TLMaterialFactory.h"
#include "TrafficLights/TLMeshFactory.h"
#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLPole.h"
#include "UObject/NameTypes.h"
#include "UObject/UObjectGlobals.h"

void STrafficLightPreviewViewport::Construct(const FArguments& InArgs)
{
	LightTypesTable = FTLMeshFactory::GetLightTypeMeshTable();
	ModulesTable = FTLMeshFactory::GetModuleMeshTable();
	PolesTable = FTLMeshFactory::GetPoleMeshTable();

	if (!IsValid(LightTypesTable))
	{
		UE_LOG(LogTemp, Error, TEXT("LightTypesTable is not valid"));
	}
	if (!IsValid(ModulesTable))
	{
		UE_LOG(LogTemp, Error, TEXT("ModulesTable is not valid"));
	}
	if (!IsValid(PolesTable))
	{
		UE_LOG(LogTemp, Error, TEXT("PolesTable is not valid"));
	}

	PreviewScene = MakeUnique<FPreviewScene>(FPreviewScene::ConstructionValues());

	ViewportClient = MakeShareable(new FEditorViewportClient(nullptr, PreviewScene.Get(), nullptr));
	ViewportClient->bSetListenerPosition = false;
	ViewportClient->SetRealtime(false);
	ViewportClient->SetViewLocation(FVector(-300, 0, 150));
	ViewportClient->SetViewRotation(FRotator(0, 0, 0));
	ViewportClient->SetViewMode(VMI_Lit);
	ViewportClient->SetAllowCinematicControl(true);
	ViewportClient->VisibilityDelegate.BindLambda([]() { return true; });
	ViewportClient->EngineShowFlags.SetGrid(true);

	SAssignNew(ViewportWidget, SViewport).EnableGammaCorrection(false).EnableBlending(true);

	SceneViewport = MakeShareable(new FSceneViewport(ViewportClient.Get(), ViewportWidget));
	ViewportClient->Viewport = SceneViewport.Get();
	ViewportWidget->SetViewportInterface(SceneViewport.ToSharedRef());

	ChildSlot[ViewportWidget.ToSharedRef()];
}

STrafficLightPreviewViewport::~STrafficLightPreviewViewport()
{
	if (ViewportClient.IsValid())
	{
		FlushRenderingCommands();
		PreviewScene.Reset();
		ViewportClient->Viewport = nullptr;
	}
}

UStaticMeshComponent* STrafficLightPreviewViewport::AddModuleMesh(const FTLPole& Pole, const FTLHead& Head, FTLModule& ModuleData)
{
	const FTransform ModuleWorldTransform{ModuleData.Transform * Head.Transform * Pole.Transform * ModuleData.Offset};

	UWorld* World{PreviewScene->GetWorld()};
	UObject* LevelOuter{World->PersistentLevel};
	UStaticMeshComponent* Comp{NewObject<UStaticMeshComponent>(LevelOuter, NAME_None, RF_Transient)};
	if (!Comp)
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to create StaticMeshComponent for module"));
		return nullptr;
	}
	PreviewScene->AddComponent(Comp, ModuleWorldTransform);
	Comp->SetStaticMesh(ModuleData.ModuleMesh);

	int32 LightIndex{0};
	for (FTLModuleLight& Light : ModuleData.Lights)
	{
		if (Light.LightMID == nullptr)
		{
			Light.LightMID = FMaterialFactory::GetLightMaterialInstance(Comp);
		}
		if (Light.LightMID)
		{
			Light.LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light.EmissiveIntensity);
			Light.LightMID->SetVectorParameterValue(TEXT("Emissive Color"), Light.EmissiveColor);
			Light.LightMID->SetScalarParameterValue(TEXT("Offset U"), static_cast<float>(Light.U));
			Light.LightMID->SetScalarParameterValue(TEXT("Offset Y"), static_cast<float>(Light.V));
			const FName MaterialSlotName{FString::Printf(TEXT("led_%d"), LightIndex++)};
			Comp->SetMaterialByName(MaterialSlotName, Light.LightMID);
		}
	}

	ModuleMeshComponents.Add(Comp);

	return Comp;
}

UStaticMeshComponent* STrafficLightPreviewViewport::AddPoleBaseMesh(const FTLPole& Pole)
{
	const FTransform PoleWorldTransform{Pole.Transform};

	UWorld* World{PreviewScene->GetWorld()};
	UObject* LevelOuter{World->PersistentLevel};
	UStaticMeshComponent* Comp{NewObject<UStaticMeshComponent>(LevelOuter, NAME_None, RF_Transient)};
	if (!Comp)
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to create StaticMeshComponent for pole"));
		return nullptr;
	}
	PreviewScene->AddComponent(Comp, PoleWorldTransform);
	Comp->SetStaticMesh(Pole.BasePoleMesh);
	PoleBaseMeshComponents.Add(Comp);

	return Comp;
}

UStaticMeshComponent* STrafficLightPreviewViewport::AddPoleExtensibleMesh(const FTLPole& Pole)
{
	const FTransform PoleWorldTransform{Pole.Transform * Pole.Offset};

	UWorld* World{PreviewScene->GetWorld()};
	UObject* LevelOuter{World->PersistentLevel};
	UStaticMeshComponent* Comp{NewObject<UStaticMeshComponent>(LevelOuter, NAME_None, RF_Transient)};
	if (!Comp)
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to create StaticMeshComponent for pole"));
		return nullptr;
	}
	PreviewScene->AddComponent(Comp, PoleWorldTransform);
	Comp->SetStaticMesh(Pole.ExtendiblePoleMesh);
	PoleExtensibleMeshComponents.Add(Comp);

	return Comp;
}

UStaticMeshComponent* STrafficLightPreviewViewport::AddPoleCapMesh(const FTLPole& Pole)
{
	const FTransform PoleWorldTransform{Pole.Transform};

	UWorld* World{PreviewScene->GetWorld()};
	UObject* LevelOuter{World->PersistentLevel};
	UStaticMeshComponent* Comp{NewObject<UStaticMeshComponent>(LevelOuter, NAME_None, RF_Transient)};
	if (!Comp)
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to create StaticMeshComponent for pole"));
		return nullptr;
	}
	PreviewScene->AddComponent(Comp, PoleWorldTransform);
	Comp->SetStaticMesh(Pole.CapPoleMesh);
	PoleCapMeshComponents.Add(Comp);

	return Comp;
}

void STrafficLightPreviewViewport::AddBackplate(const FTLPole& Pole, const FTLHead& Head)
{
	// 1) Compute head’s world transform and orientation
	const FTransform HeadWorldTransform{Head.Transform * Pole.Transform};
	const FVector HeadCenterLocation{HeadWorldTransform.GetLocation()};
	const FQuat HeadWorldRotation{HeadWorldTransform.GetRotation()};

	// 2) Load the four backplate meshes
	UStaticMesh* CornerMesh{FTLMeshFactory::GetBackplateCornerMesh(Head)};
	UStaticMesh* HorizontalMesh{FTLMeshFactory::GetBackplateHorizontalMesh(Head)};
	UStaticMesh* VerticalMesh{FTLMeshFactory::GetBackplateVerticalMesh(Head)};
	UStaticMesh* MiddleMesh{FTLMeshFactory::GetBackplateMiddleMesh(Head)};
	check(CornerMesh && HorizontalMesh && VerticalMesh && MiddleMesh);

	// 3) Compute the bounding box of the head, in world space
	FBox HeadBounds(ForceInit);
	for (const FTLModule& Module : Head.Modules)
	{
		if (Module.ModuleMeshComponent)
		{
			const FTransform& ModuleTransform{Module.ModuleMeshComponent->GetComponentTransform()};
			HeadBounds += Module.ModuleMesh->GetBoundingBox().TransformBy(ModuleTransform);
		}
	}
	const FVector BoundsMin{HeadBounds.Min};
	const FVector BoundsMax{HeadBounds.Max};

	struct FMeshPlacement
	{
		FVector Location;
		FRotator Rotation;
	};

	TArray<FMeshPlacement> CornerPlacements{{FVector{BoundsMin.X, BoundsMin.Y, BoundsMax.Z}, FRotator{0.0, 0.0, 0.0}},
		{FVector{BoundsMin.X, BoundsMin.Y, BoundsMin.Z}, FRotator{90.0, 0.0, 0.0}},
		{FVector{BoundsMax.X, BoundsMin.Y, BoundsMin.Z}, FRotator{180.0, 0.0, 0.0}},
		{FVector{BoundsMax.X, BoundsMin.Y, BoundsMax.Z}, FRotator{270.0, 0.0, 0.0}}};

	for (const FMeshPlacement& Placement : CornerPlacements)
	{
		UStaticMeshComponent* CornerComponent{
			NewObject<UStaticMeshComponent>(PreviewScene->GetWorld()->PersistentLevel, NAME_None, RF_Transient)};
		CornerComponent->SetStaticMesh(CornerMesh);

		const FTransform Transform{Placement.Rotation, Placement.Location, FVector{1.0, 1.0, 1.0}};
		PreviewScene->AddComponent(CornerComponent, Transform);
		BackplateComponents.Add(CornerComponent);
	}

	const double ScaleZ{(BoundsMax.Z - BoundsMin.Z) / (HorizontalMesh->GetBoundingBox().GetExtent().Z * 2.0)};
	TArray<FMeshPlacement> VerticalPlacements{{FVector{BoundsMin.X, BoundsMin.Y, BoundsMin.Z}, FRotator{0.0, 0.0, 0.0}},
		{FVector{BoundsMax.X, BoundsMin.Y, BoundsMax.Z}, FRotator{180.0, 0.0, 0.0}}};

	for (const FMeshPlacement& Placement : VerticalPlacements)
	{
		UStaticMeshComponent* VerticalComponent{
			NewObject<UStaticMeshComponent>(PreviewScene->GetWorld()->PersistentLevel, NAME_None, RF_Transient)};
		VerticalComponent->SetStaticMesh(VerticalMesh);

		const FTransform Transform{Placement.Rotation, Placement.Location, FVector{1.0, 1.0, ScaleZ}};
		PreviewScene->AddComponent(VerticalComponent, Transform);
		BackplateComponents.Add(VerticalComponent);
	}

	const double ScaleX{(BoundsMax.X - BoundsMin.X) / (HorizontalMesh->GetBoundingBox().GetExtent().X * 2.0)};
	TArray<FMeshPlacement> HorizontalPlacements{{FVector{BoundsMax.X, BoundsMin.Y, BoundsMax.Z}, FRotator{0.0, 0.0, 0.0}},
		{FVector{BoundsMin.X, BoundsMin.Y, BoundsMin.Z}, FRotator{180.0, 0.0, 0.0}}};

	for (const FMeshPlacement& Placement : HorizontalPlacements)
	{
		UStaticMeshComponent* HorizontalComponent{
			NewObject<UStaticMeshComponent>(PreviewScene->GetWorld()->PersistentLevel, NAME_None, RF_Transient)};
		HorizontalComponent->SetStaticMesh(HorizontalMesh);

		const FTransform Transform{Placement.Rotation, Placement.Location, FVector{ScaleX, 1.0, 1.0}};
		PreviewScene->AddComponent(HorizontalComponent, Transform);
		BackplateComponents.Add(HorizontalComponent);
	}

	{
		UStaticMeshComponent* MiddleComponent{
			NewObject<UStaticMeshComponent>(PreviewScene->GetWorld()->PersistentLevel, NAME_None, RF_Transient)};
		MiddleComponent->SetStaticMesh(MiddleMesh);
		const FTransform Transform{HeadWorldRotation, FVector{BoundsMax.X, BoundsMin.Y, BoundsMin.Z}, FVector{ScaleX, 1.0, ScaleZ}};
		PreviewScene->AddComponent(MiddleComponent, Transform);
		BackplateComponents.Add(MiddleComponent);
	}
}

void STrafficLightPreviewViewport::ClearModuleMeshes()
{
	for (UStaticMeshComponent* Comp : ModuleMeshComponents)
	{
		if (Comp)
		{
			PreviewScene->RemoveComponent(Comp);
			Comp->DestroyComponent();
		}
	}
	ModuleMeshComponents.Empty();
}

void STrafficLightPreviewViewport::ClearPoleMeshes()
{
	for (UStaticMeshComponent* Comp : PoleBaseMeshComponents)
	{
		if (Comp)
		{
			PreviewScene->RemoveComponent(Comp);
			Comp->DestroyComponent();
		}
	}
	for (UStaticMeshComponent* Comp : PoleExtensibleMeshComponents)
	{
		if (Comp)
		{
			PreviewScene->RemoveComponent(Comp);
			Comp->DestroyComponent();
		}
	}
	for (UStaticMeshComponent* Comp : PoleCapMeshComponents)
	{
		if (Comp)
		{
			PreviewScene->RemoveComponent(Comp);
			Comp->DestroyComponent();
		}
	}
	PoleBaseMeshComponents.Empty();
	PoleExtensibleMeshComponents.Empty();
	PoleCapMeshComponents.Empty();
}

void STrafficLightPreviewViewport::ClearBackplates()
{
	for (UStaticMeshComponent* Comp : BackplateComponents)
	{
		if (Comp)
		{
			PreviewScene->RemoveComponent(Comp);
			Comp->DestroyComponent();
		}
	}
	BackplateComponents.Reset();
}

void STrafficLightPreviewViewport::Rebuild(TArray<FTLPole>& Poles)
{
	ClearModuleMeshes();
	ClearPoleMeshes();
	ClearBackplates();
	for (FTLPole& Pole : Poles)
	{
		if (Pole.BasePoleMesh)
		{
			PoleBaseMeshComponents.Add(AddPoleBaseMesh(Pole));
		}
		if (Pole.ExtendiblePoleMesh)
		{
			PoleExtensibleMeshComponents.Add(AddPoleExtensibleMesh(Pole));
		}
		if (Pole.CapPoleMesh)
		{
			PoleCapMeshComponents.Add(AddPoleCapMesh(Pole));
		}
		for (FTLHead& Head : Pole.Heads)
		{
			for (FTLModule& Module : Head.Modules)
			{
				Module.ModuleMeshComponent = AddModuleMesh(Pole, Head, Module);
			}
			if (Head.bHasBackplate)
			{
				AddBackplate(Pole, Head);
			}
		}
	}
	if (SceneViewport.IsValid())
	{
		SceneViewport->Invalidate();
	}
}

void STrafficLightPreviewViewport::ResetFrame(const UStaticMeshComponent* Comp)
{
	if (!Comp)
	{
		UE_LOG(LogTemp, Error, TEXT("ResetFrame: Invalid Comp"));
		return;
	}
	if (!ViewportClient.IsValid())
	{
		UE_LOG(LogTemp, Error, TEXT("ResetFrame: Invalid ViewportClient"));
		return;
	}

	FBox Box(EForceInit::ForceInit);
	Box += Comp->Bounds.GetBox();

	const FVector Center{Box.GetCenter()};
	const double Radius{Box.GetExtent().GetMax()};
	const double Distance{Radius * -10.0};
	const FVector Forward{FVector::ForwardVector.Rotation().RotateVector(FVector(0, 1, 0))};
	const FVector Up{FVector::UpVector};
	const FVector CamPos{Center - Forward * Distance + Up * (Radius * 0.5)};
	const FRotator CamRot(0.0, -90.0, 0.0);

	ViewportClient->SetViewLocation(CamPos);
	ViewportClient->SetViewRotation(CamRot);
}
