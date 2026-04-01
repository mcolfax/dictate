cask "dictate" do
  version "1.4.8"
  sha256 "b3b7c1ec496c2a6b6dbdd2a8764f4f10359e0bad5cdab2eda2df5d254d8a3230"

  url "https://github.com/mcolfax/dictate/releases/download/v#{version}/Dictate.dmg"
  name "Dictate"
  desc "Free local AI dictation for macOS — no cloud, no subscription"
  homepage "https://github.com/mcolfax/dictate"

  depends_on macos: ">= :monterey"
  depends_on arch: :arm64

  app "Dictate.app"

  zap trash: [
    "~/.dictate",
    "~/Library/Logs/Dictate",
  ]
end
