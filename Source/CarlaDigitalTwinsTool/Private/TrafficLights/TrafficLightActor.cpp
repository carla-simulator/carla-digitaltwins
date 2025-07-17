#include "TrafficLights/TrafficLightActor.h"

#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshSocket.h"
#include "Generation/MapGenFunctionLibrary.h"
#include "Logging/LogMacros.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Math/MathFwd.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLMaterialFactory.h"
#include "TrafficLights/TLMeshFactory.h"
#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLPole.h"

ATrafficLightActor::ATrafficLightActor()
{
	RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
}

void ATrafficLightActor::BuildFromPoles()
{
	{
		TArray<UActorComponent*> Old;
		GetComponents(Old);
		for (UActorComponent* C : Old)
		{
			if (C != RootComponent)
			{
				C->DestroyComponent();
				RemoveInstanceComponent(C);
			}
		}
	}

	for (FTLPole& Pole : Poles)
	{
		USceneComponent* PoleRoot{AddPoleBase(Pole)};
		AddPoleExtensible(PoleRoot, Pole);
		AddPoleCap(PoleRoot, Pole);

		for (FTLHead& Head : Pole.Heads)
		{
			USceneComponent* HeadRoot{AddHead(PoleRoot, Pole, Head)};

			for (FTLModule& Mod : Head.Modules)
			{
				AddModule(HeadRoot, Pole, Head, Mod);
			}
			RebuildModuleChain(Head);
			if (Head.bHasBackplate)
			{
				AddBackplate(HeadRoot, Pole, Head);
			}
		}
	}
}

USceneComponent* ATrafficLightActor::AddHead(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head)
{
	USceneComponent* HeadRoot{UMapGenFunctionLibrary::AddSceneComponentToActor(this)};

	HeadRoot->SetupAttachment(Parent);
	HeadRoot->RegisterComponent();
	HeadRoot->SetWorldTransform(Head.Transform * Pole.Transform);

	return HeadRoot;
}

UStaticMeshComponent* ATrafficLightActor::AddModule(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head, FTLModule& ModuleData)
{
	const FTransform ModuleTransform{ModuleData.Transform * Head.Transform * Pole.Transform * ModuleData.Offset};

	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!Comp)
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to create StaticMeshComponent for module"));
		return nullptr;
	}
	Comp->SetupAttachment(Parent);
	Comp->SetWorldTransform(ModuleTransform);
	Comp->SetStaticMesh(ModuleData.ModuleMesh);

	UMaterialInterface* BaseMat{FMaterialFactory::GetLightMaterialInstance(Comp)};

	int32 LightIndex{0};
	for (FTLModuleLight& Light : ModuleData.Lights)
	{
		const FName MaterialSlotName{FString::Printf(TEXT("led_%d"), LightIndex++)};
		const int32 MaterialIndex{Comp->GetMaterialIndex(MaterialSlotName)};
		Light.LightMID = Comp->CreateDynamicMaterialInstance(MaterialIndex, BaseMat);
		Light.LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light.EmissiveIntensity);
		Light.LightMID->SetVectorParameterValue(TEXT("Emissive Color"), Light.EmissiveColor);
		Light.LightMID->SetScalarParameterValue(TEXT("Offset U"), static_cast<float>(Light.U));
		Light.LightMID->SetScalarParameterValue(TEXT("Offset Y"), static_cast<float>(Light.V));
	}

	Comp->Modify();
	ModuleMeshComponents.Add(Comp);
	ModuleData.Transform = ModuleTransform;
	ModuleData.ModuleMeshComponent = Comp;
	return Comp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleBase(FTLPole& Pole)
{
	const FTransform ModuleTransform{Pole.Transform};
	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	Comp->SetupAttachment(RootComponent);
	Comp->RegisterComponent();
	Comp->SetWorldTransform(ModuleTransform);
	Comp->SetStaticMesh(Pole.BasePoleMesh);
	Comp->Modify();

	Pole.BasePoleMeshComponent = Comp;
	return Comp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleExtensible(USceneComponent* Parent, FTLPole& Pole)
{
	const FTransform ModuleTransform{Pole.Transform};
	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	Comp->SetupAttachment(Parent);
	Comp->RegisterComponent();
	Comp->SetWorldTransform(ModuleTransform);
	Comp->SetStaticMesh(Pole.ExtendiblePoleMesh);
	Comp->Modify();

	Pole.ExtendiblePoleMeshComponent = Comp;
	return Comp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleCap(USceneComponent* Parent, FTLPole& Pole)
{
	const FTransform ModuleTransform{Pole.Transform};
	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	Comp->SetupAttachment(Parent);
	Comp->RegisterComponent();
	Comp->SetWorldTransform(ModuleTransform);
	Comp->SetStaticMesh(Pole.CapPoleMesh);
	Comp->Modify();

	Pole.CapPoleMeshComponent = Comp;
	return Comp;
}

void ATrafficLightActor::AddBackplate(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head)
{
	const FTransform HeadWorld{Head.Transform * Pole.Transform};
	const FVector HeadCenter{HeadWorld.GetLocation()};
	const FQuat HeadRotation{HeadWorld.GetRotation()};

	UStaticMesh* CornerMesh{FTLMeshFactory::GetBackplateCornerMesh(Head)};
	UStaticMesh* HorizontalMesh{FTLMeshFactory::GetBackplateHorizontalMesh(Head)};
	UStaticMesh* VerticalMesh{FTLMeshFactory::GetBackplateVerticalMesh(Head)};
	UStaticMesh* MiddleMesh{FTLMeshFactory::GetBackplateMiddleMesh(Head)};
	if (!(CornerMesh && HorizontalMesh && VerticalMesh && MiddleMesh))
	{
		return;
	}

	FBox HeadBounds(ForceInit);
	for (const FTLModule& Module : Head.Modules)
	{
		if (Module.ModuleMeshComponent)
		{
			const FTransform& ModTF = Module.ModuleMeshComponent->GetComponentTransform();
			HeadBounds += Module.ModuleMesh->GetBoundingBox().TransformBy(ModTF);
		}
	}
	const FVector BoundsMin{HeadBounds.Min};
	const FVector BoundsMax{HeadBounds.Max};

	auto SpawnBackplatePiece = [&](UStaticMesh* Mesh, const FVector& LocWS, const FRotator& RotWS, const FVector& ScaleWS)
	{
		UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
		Comp->SetStaticMesh(Mesh);
		Comp->SetupAttachment(Parent);
		Comp->RegisterComponent();
		Comp->SetWorldTransform(FTransform(RotWS, LocWS, ScaleWS));
	};

	struct FPlacement
	{
		FVector Loc;
		FRotator Rot;
	};
	const TArray<FPlacement> Corners = {{{BoundsMin.X, BoundsMin.Y, BoundsMax.Z}, {0.f, 0.f, 0.f}},
		{{BoundsMin.X, BoundsMin.Y, BoundsMin.Z}, {90.f, 0.f, 0.f}}, {{BoundsMax.X, BoundsMin.Y, BoundsMin.Z}, {180.f, 0.f, 0.f}},
		{{BoundsMax.X, BoundsMin.Y, BoundsMax.Z}, {270.f, 0.f, 0.f}}};

	for (const FPlacement& P : Corners)
	{
		SpawnBackplatePiece(CornerMesh, P.Loc, P.Rot, FVector::OneVector);
	}

	/* ───────────────────────────────── 5) Verticales ────────────────────────────────────── */
	const float ScaleZ{(BoundsMax.Z - BoundsMin.Z) / (HorizontalMesh->GetBoundingBox().GetExtent().Z * 2.f)};

	const TArray<FPlacement> Verticals = {
		{{BoundsMin.X, BoundsMin.Y, BoundsMin.Z}, {0.f, 0.f, 0.f}}, {{BoundsMax.X, BoundsMin.Y, BoundsMax.Z}, {180.f, 0.f, 0.f}}};

	for (const FPlacement& P : Verticals)
	{
		SpawnBackplatePiece(VerticalMesh, P.Loc, P.Rot, FVector{1.f, 1.f, ScaleZ});
	}

	const float ScaleX{(BoundsMax.X - BoundsMin.X) / (HorizontalMesh->GetBoundingBox().GetExtent().X * 2.f)};

	const TArray<FPlacement> Horizontals = {
		{{BoundsMax.X, BoundsMin.Y, BoundsMax.Z}, {0.f, 0.f, 0.f}}, {{BoundsMin.X, BoundsMin.Y, BoundsMin.Z}, {180.f, 0.f, 0.f}}};

	for (const FPlacement& P : Horizontals)
	{
		SpawnBackplatePiece(HorizontalMesh, P.Loc, P.Rot, FVector{ScaleX, 1.f, 1.f});
	}

	SpawnBackplatePiece(MiddleMesh, FVector{BoundsMax.X, BoundsMin.Y, BoundsMin.Z},
		HeadRotation.Rotator(),	   // misma rot que el head
		FVector{ScaleX, 1.f, ScaleZ});
}

void ATrafficLightActor::RebuildModuleChain(FTLHead& Head)
{
	if (Head.Modules.IsEmpty())
	{
		return;
	}

	static const FName EndSocketName{FName("Socket2")};
	static const FName BeginSocketName{FName("Socket1")};

	{
		FTLModule& Module{Head.Modules[0]};
		Module.Transform = FTransform::Identity;
		if (Module.ModuleMeshComponent)
		{
			Module.ModuleMeshComponent->SetRelativeTransform(Module.Transform * Head.Transform * Module.Offset);
		}
	}

	for (int32 i{1}; i < Head.Modules.Num(); ++i)
	{
		const FTLModule& Prev{Head.Modules[i - 1]};
		FTLModule& Curr{Head.Modules[i]};

		if (!IsValid(Prev.ModuleMeshComponent))
		{
			UE_LOG(LogTemp, Warning, TEXT("Previous module mesh component is invalid at module %d"), i);
			continue;
		}
		if (!IsValid(Curr.ModuleMeshComponent))
		{
			UE_LOG(LogTemp, Warning, TEXT("Missing component at module %d"), i);
			continue;
		}

		const UStaticMeshSocket* PrevSocket{Prev.ModuleMesh->FindSocket(EndSocketName)};
		const UStaticMeshSocket* CurrSocket{Curr.ModuleMesh->FindSocket(BeginSocketName)};
		if (!IsValid(PrevSocket))
		{
			UE_LOG(LogTemp, Warning, TEXT("Previous socket not found at module %d"), i);
			continue;
		}
		if (!IsValid(CurrSocket))
		{
			UE_LOG(LogTemp, Warning, TEXT("Current socket not found at module %d"), i);
			continue;
		}

		const FTransform PrevBase{Prev.Transform * Prev.Offset};
		const FTransform PrevLocal(FQuat::Identity, PrevSocket->RelativeLocation, FVector::OneVector);
		const FTransform CurrLocal(FQuat::Identity, CurrSocket->RelativeLocation, FVector::OneVector);
		const FTransform SnapDelta{PrevBase * PrevLocal * CurrLocal.Inverse()};
		Curr.Transform = SnapDelta;
		Curr.ModuleMeshComponent->SetRelativeTransform(Curr.Transform * Head.Transform * Curr.Offset);
	}
}
