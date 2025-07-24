#include "TrafficLights/TrafficLightActor.h"

#include "CarlaDigitalTwinsTool.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Dom/JsonObject.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshSocket.h"
#include "Generation/MapGenFunctionLibrary.h"
#include "JsonUtilities.h"
#include "Logging/LogMacros.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Math/MathFwd.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Templates/SharedPointer.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLMaterialFactory.h"
#include "TrafficLights/TLMeshFactory.h"
#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLPole.h"

namespace
{
FTransform ParseTransform(const TSharedPtr<FJsonObject>& Json)
{
	const auto& Loc{Json->GetObjectField(TEXT("Location"))};
	const auto& Rot{Json->GetObjectField(TEXT("Rotation"))};
	const auto& Scale{Json->GetObjectField(TEXT("Scale"))};
	const FVector Location(Loc->GetNumberField(TEXT("X")), Loc->GetNumberField(TEXT("Y")), Loc->GetNumberField(TEXT("Z")));
	const FRotator Rotation(
		Rot->GetNumberField(TEXT("Pitch")), Rot->GetNumberField(TEXT("Yaw")), Rot->GetNumberField(TEXT("Roll")));
	const FVector Scale3D(Scale->GetNumberField(TEXT("X")), Scale->GetNumberField(TEXT("Y")), Scale->GetNumberField(TEXT("Z")));
	return FTransform(Rotation, Location, Scale3D);
}
template <typename TEnum>
TEnum GetEnumValueFromString(const FString& Name)
{
	if (UEnum* Enum = StaticEnum<TEnum>())
	{
		int64 Value = Enum->GetValueByNameString(Name);
		if (Value != INDEX_NONE)
		{
			return static_cast<TEnum>(Value);
		}
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Invalid enum name '%s' for %s"), *Name, *Enum->GetName());
	}
	return TEnum(0);
}
}	 // namespace

ATrafficLightActor::ATrafficLightActor()
{
	RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
}

void ATrafficLightActor::OnConstruction(const FTransform& Transform)
{
	Super::OnConstruction(Transform);
	// Build();
}

void ATrafficLightActor::Build()
{
	Clear();
	for (FTLPole& Pole : Poles)
	{
		USceneComponent* PoleRoot{AddRootPole(RootComponent, Pole)};
		AddPoleBase(PoleRoot, Pole);
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
// TODO: Los materiales no se están aplicando a los módulos, revisar
void ATrafficLightActor::BuildFromJSON()
{
	if (JSONFile.FilePath.IsEmpty())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("JSONFile was not assigned."));
		return;
	}

	const FString FullPath{FPaths::ConvertRelativePathToFull(JSONFile.FilePath)};
	FString JSONConfig;
	if (!FFileHelper::LoadFileToString(JSONConfig, *FullPath))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to load JSON file: %s"), *FullPath);
		return;
	}

	TSharedPtr<FJsonObject> Root;
	TSharedRef<TJsonReader<TCHAR>> Reader{TJsonReaderFactory<TCHAR>::Create(JSONConfig)};
	if (!FJsonSerializer::Deserialize(Reader, Root) || !Root.IsValid())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to parse JSONConfig."));
		return;
	}

	Poles.Empty();
	const TArray<TSharedPtr<FJsonValue>>* JsonPoles;
	if (Root->TryGetArrayField(TEXT("Poles"), JsonPoles))
	{
		for (const auto& PoleValue : *JsonPoles)
		{
			const TSharedPtr<FJsonObject> PoleObj{PoleValue->AsObject()};
			FTLPole Pole;
			Pole.Transform = ParseTransform(PoleObj->GetObjectField(TEXT("Transform")));
			Pole.Offset = ParseTransform(PoleObj->GetObjectField(TEXT("Offset")));
			Pole.Style = GetEnumValueFromString<ETLStyle>(PoleObj->GetStringField(TEXT("Style")));
			Pole.Orientation = GetEnumValueFromString<ETLOrientation>(PoleObj->GetStringField(TEXT("Orientation")));
			Pole.PoleHeight = PoleObj->GetNumberField(TEXT("PoleHeight"));

			Pole.BasePoleMesh = FTLMeshFactory::GetBaseMeshForPole(Pole);
			Pole.ExtendiblePoleMesh = FTLMeshFactory::GetExtendibleMeshForPole(Pole);

			const TArray<TSharedPtr<FJsonValue>>* JsonHeads;
			if (PoleObj->TryGetArrayField(TEXT("Heads"), JsonHeads))
			{
				for (const auto& HeadValue : *JsonHeads)
				{
					const TSharedPtr<FJsonObject> HeadObj{HeadValue->AsObject()};
					FTLHead Head;
					Head.Transform = ParseTransform(HeadObj->GetObjectField(TEXT("Transform")));
					Head.Offset = ParseTransform(HeadObj->GetObjectField(TEXT("Offset")));
					Head.Style = GetEnumValueFromString<ETLStyle>(HeadObj->GetStringField(TEXT("Style")));
					Head.Attachment = GetEnumValueFromString<ETLHeadAttachment>(HeadObj->GetStringField(TEXT("Attachment")));
					Head.Orientation = GetEnumValueFromString<ETLOrientation>(HeadObj->GetStringField(TEXT("Orientation")));
					Head.bHasBackplate = HeadObj->GetBoolField(TEXT("bHasBackplate"));

					const TArray<TSharedPtr<FJsonValue>>* JsonModules;
					if (HeadObj->TryGetArrayField(TEXT("Modules"), JsonModules))
					{
						for (const auto& ModValue : *JsonModules)
						{
							const TSharedPtr<FJsonObject> ModObj{ModValue->AsObject()};
							FTLModule Module;
							Module.Transform = ParseTransform(ModObj->GetObjectField(TEXT("Transform")));
							Module.Offset = ParseTransform(ModObj->GetObjectField(TEXT("Offset")));
							Module.bHasVisor = ModObj->GetBoolField(TEXT("bHasVisor"));
							Module.ModuleMesh = FTLMeshFactory::GetAllMeshesForModule(Head, Module).Last();
							const TArray<TSharedPtr<FJsonValue>>* JsonLights;
							if (ModObj->TryGetArrayField(TEXT("Lights"), JsonLights))
							{
								for (const auto& LightValue : *JsonLights)
								{
									const TSharedPtr<FJsonObject> LightObj{LightValue->AsObject()};
									FTLModuleLight Light;
									Light.LightType =
										GetEnumValueFromString<ETLLightType>(LightObj->GetStringField(TEXT("LightType")));
									const FVector2D AtlasUV{FTLMeshFactory::GetAtlasCoordsForLightType(Light.LightType)};
									Light.U = AtlasUV.X;
									Light.V = AtlasUV.Y;
									Light.EmissiveIntensity = LightObj->GetNumberField(TEXT("EmissiveIntensity"));
									const auto& ColorObj{LightObj->GetObjectField(TEXT("EmissiveColor"))};
									Light.EmissiveColor.R = ColorObj->GetNumberField(TEXT("R"));
									Light.EmissiveColor.G = ColorObj->GetNumberField(TEXT("G"));
									Light.EmissiveColor.B = ColorObj->GetNumberField(TEXT("B"));
									Light.EmissiveColor.A = ColorObj->GetNumberField(TEXT("A"));
									Module.Lights.Add(MoveTemp(Light));
								}
							}
							Head.Modules.Add(MoveTemp(Module));
						}
					}
					Pole.Heads.Add(MoveTemp(Head));
				}
			}
			Poles.Add(MoveTemp(Pole));
		}
	}
	Build();
}

USceneComponent* ATrafficLightActor::AddRootPole(USceneComponent* Parent, FTLPole& Pole)
{
	USceneComponent* PoleRoot{UMapGenFunctionLibrary::AddSceneComponentToActor(this)};
	if (!PoleRoot)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("AddRootPole: no pudo crear PoleRoot"));
		return nullptr;
	}

	PoleRoot->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	PoleRoot->SetRelativeTransform(Pole.Transform);

	return PoleRoot;
}

USceneComponent* ATrafficLightActor::AddHead(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head)
{
	USceneComponent* HeadRoot{UMapGenFunctionLibrary::AddSceneComponentToActor(this)};

	HeadRoot->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	HeadRoot->SetRelativeTransform(Head.Transform);

	return HeadRoot;
}

UStaticMeshComponent* ATrafficLightActor::AddModule(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head, FTLModule& ModuleData)
{
	const FTransform ModuleTransform{ModuleData.Transform * ModuleData.Offset};

	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!Comp)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to create StaticMeshComponent for module"));
		return nullptr;
	}
	Comp->SetStaticMesh(ModuleData.ModuleMesh);
	Comp->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	Comp->SetRelativeTransform(ModuleTransform);

	UMaterialInterface* BaseMat{FMaterialFactory::GetLightMaterialInstance(Comp)};

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

	Comp->Modify();
	ModuleMeshComponents.Add(Comp);
	ModuleData.Transform = ModuleTransform;
	ModuleData.ModuleMeshComponent = Comp;
	return Comp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleBase(USceneComponent* Parent, FTLPole& Pole)
{
	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!Comp)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to create StaticMeshComponent for pole base"));
		return nullptr;
	}

	Comp->SetStaticMesh(Pole.BasePoleMesh);
	Comp->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	Comp->SetRelativeTransform(Pole.Offset);
	Comp->Modify();

	Pole.BasePoleMeshComponent = Comp;
	return Comp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleExtensible(USceneComponent* Parent, FTLPole& Pole)
{
	UStaticMeshComponent* BaseComp{Pole.BasePoleMeshComponent};
	if (!BaseComp)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("AddPoleExtensible: BasePoleMeshComponent not set"));
		return nullptr;
	}

	UStaticMeshComponent* ExtComp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!ExtComp)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("AddPoleExtensible: unable to create ExtComp"));
		return nullptr;
	}
	ExtComp->SetStaticMesh(Pole.ExtendiblePoleMesh);
	static const FName SocketName("extensible");
	ExtComp->AttachToComponent(BaseComp, FAttachmentTransformRules::SnapToTargetIncludingScale, SocketName);
	const double BaseLengthZ{BaseComp->GetStaticMesh()->GetBounds().BoxExtent.Z * 2.0};
	const double ExtLengthZ{Pole.ExtendiblePoleMesh->GetBounds().BoxExtent.Z * 2.0};
	if (ExtLengthZ > KINDA_SMALL_NUMBER)
	{
		const double DesiredExtHeight{FMath::Max(Pole.PoleHeight - BaseLengthZ, 0.0)};
		const double ScaleZ{DesiredExtHeight / ExtLengthZ};
		FVector Scale3D = ExtComp->GetRelativeScale3D();
		Scale3D.Z = ScaleZ;
		ExtComp->SetRelativeScale3D(Scale3D);
	}

	ExtComp->Modify();
	Pole.ExtendiblePoleMeshComponent = ExtComp;
	return ExtComp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleCap(USceneComponent* Parent, FTLPole& Pole)
{
	UStaticMeshComponent* ExtensibleComp{Pole.BasePoleMeshComponent};
	if (!ExtensibleComp)
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("AddPoleExtensible: BasePoleMeshComponent not set"));
		return nullptr;
	}

	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!Comp)
	{
		return nullptr;
	}

	Comp->SetStaticMesh(Pole.CapPoleMesh);
	static const FName CapSocketName(TEXT("cap"));
	Comp->AttachToComponent(ExtensibleComp, FAttachmentTransformRules::SnapToTargetIncludingScale, CapSocketName);
	Comp->Modify();

	Pole.CapPoleMeshComponent = Comp;
	return Comp;
}

void ATrafficLightActor::AddBackplate(USceneComponent* HeadRoot, FTLPole& Pole, FTLHead& Head)
{
	// TODO: hacer que las horizontes y verticales sean extensibles y que los corners siempre
	// TODO: los corners deben estar "attachaddos" a las verticales y horizontales, de forma que si extienden las horizontales y
	// verticales los corners se ajusten a la nueva longitud
	// por defecto el extiensible es del tamanyo justo para  que sobrasalgan los corners
	FBox LocalBounds{ForceInit};
	for (const FTLModule& Module : Head.Modules)
	{
		if (!Module.ModuleMeshComponent)
		{
			continue;
		}

		const FBox MeshBB{Module.ModuleMeshComponent->GetStaticMesh()->GetBoundingBox()};
		LocalBounds += MeshBB.TransformBy(Module.ModuleMeshComponent->GetRelativeTransform());
	}
	const FVector LMin{LocalBounds.Min};
	const FVector LMax{LocalBounds.Max};

	const double LocalHeight{LMax.Z - LMin.Z};
	const double ZScaleVert{LocalHeight / (FTLMeshFactory::GetBackplateVerticalMesh(Head)->GetBoundingBox().GetExtent().Z * 2.0)};

	const double LocalWidth{LMax.X - LMin.X};
	const double XScaleHorz{LocalWidth / (FTLMeshFactory::GetBackplateHorizontalMesh(Head)->GetBoundingBox().GetExtent().X * 2.0)};

	auto SpawnLocalPiece = [&](UStaticMesh* Mesh, const FVector& Loc, const FRotator& Rot, const FVector& Scale)
	{
		UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
		if (!IsValid(Comp))
		{
			return;
		}

		Comp->SetStaticMesh(Mesh);
		Comp->AttachToComponent(HeadRoot, FAttachmentTransformRules::KeepRelativeTransform);

		const double HalfDepth{Mesh->GetBoundingBox().GetExtent().Y};
		const FVector AdjustLoc{Loc.X, Loc.Y - HalfDepth, Loc.Z};

		Comp->SetRelativeLocation(AdjustLoc);
		Comp->SetRelativeRotation(Rot);
		Comp->SetRelativeScale3D(Scale);
		Comp->Modify();
	};

	struct FPlacement
	{
		FVector Loc;
		FRotator Rot;
	};
	const TArray<FPlacement> Corners{{{LMin.X, LMin.Y, LMax.Z}, {0.0, 0.0, 0.0}}, {{LMin.X, LMin.Y, LMin.Z}, {90.0, 0.0, 0.0}},
		{{LMax.X, LMin.Y, LMin.Z}, {180.0, 0.0, 0.0}}, {{LMax.X, LMin.Y, LMax.Z}, {270.0, 0.0, 0.0}}};
	for (const FPlacement& P : Corners)
	{
		SpawnLocalPiece(FTLMeshFactory::GetBackplateCornerMesh(Head), P.Loc, P.Rot, FVector{1.0, 1.0, 1.0});
	}

	const TArray<FPlacement> Verticals{
		{{LMin.X, LMin.Y, LMin.Z}, {0.0, 0.0, 0.0}}, {{LMax.X, LMin.Y, LMin.Z + LocalHeight}, {180.0, 0.0, 0.0}}};
	for (const FPlacement& P : Verticals)
	{
		SpawnLocalPiece(FTLMeshFactory::GetBackplateVerticalMesh(Head), P.Loc, P.Rot, FVector{1.0, 1.0, ZScaleVert});
	}

	const TArray<FPlacement> Horizontals{
		{{LMin.X + LocalWidth, LMin.Y, LMax.Z}, {0.0, 0.0, 0.0}}, {{LMin.X, LMin.Y, LMin.Z}, {180.0, 0.0, 0.0}}};
	for (const FPlacement& P : Horizontals)
	{
		SpawnLocalPiece(FTLMeshFactory::GetBackplateHorizontalMesh(Head), P.Loc, P.Rot, FVector{XScaleHorz, 1.0, 1.0});
	}

	const FVector CenterLoc{LMin.X + (LMax.X - LMin.X), LMin.Y, LMin.Z};
	SpawnLocalPiece(
		FTLMeshFactory::GetBackplateMiddleMesh(Head), CenterLoc, FRotator{0.0, 0.0, 0.0}, FVector{XScaleHorz, 1.0, ZScaleVert});
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
			Module.ModuleMeshComponent->SetRelativeTransform(Module.Offset);
		}
	}

	for (int32 i{1}; i < Head.Modules.Num(); ++i)
	{
		const FTLModule& Prev{Head.Modules[i - 1]};
		FTLModule& Curr{Head.Modules[i]};

		if (!IsValid(Prev.ModuleMeshComponent))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Previous module mesh component is invalid at module %d"), i);
			continue;
		}
		if (!IsValid(Curr.ModuleMeshComponent))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Missing component at module %d"), i);
			continue;
		}

		const UStaticMeshSocket* PrevSocket{Prev.ModuleMesh->FindSocket(EndSocketName)};
		const UStaticMeshSocket* CurrSocket{Curr.ModuleMesh->FindSocket(BeginSocketName)};
		if (!IsValid(PrevSocket))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Previous socket not found at module %d"), i);
			continue;
		}
		if (!IsValid(CurrSocket))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Current socket not found at module %d"), i);
			continue;
		}

		const FTransform PrevBase{Prev.Transform * Prev.Offset};
		const FTransform PrevLocal(FQuat::Identity, PrevSocket->RelativeLocation, FVector::OneVector);
		const FTransform CurrLocal(FQuat::Identity, CurrSocket->RelativeLocation, FVector::OneVector);
		const FTransform SnapDelta{PrevBase * PrevLocal * CurrLocal.Inverse()};
		Curr.Transform = SnapDelta;
		Curr.ModuleMeshComponent->SetRelativeTransform(Curr.Transform * Curr.Offset);
	}
}

void ATrafficLightActor::Clear()
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
